"""Read SQLite WAL segments: validate frames, find commit boundaries, rebuild images.

The workbench normally reads the live source through SQLite.  A retention
handoff also needs to show *why* a workbench read saw one version while a
background writer was working on another.  This module keeps that reasoning
at the byte level:

* a WAL header and each frame carry the salt and cumulative checksum that
  SQLite wrote, so a segment can be validated without trusting a timestamp;
* only frames up to the last commit frame belong to the reader-visible
  database image;
* a valid frame after the last commit is an uncommitted writer image, not
  part of what a SQLite reader (and therefore the workbench) may see.

The standalone offsite counterpart is :mod:`keelsplice.retention_verify`; it
repeats these rules without importing this package.
"""

import struct

MAGIC_LITTLE = 0x377F0682
MAGIC_BIG = 0x377F0683
WAL_VERSION = 3007000
PAGE_SIZES = frozenset((512, 1024, 2048, 4096, 8192, 16384, 32768, 65536))
MAX_PAGE = 0x7FFFFFFF
MAX_IMAGE_BYTES = 64 * 1024 * 1024


class WalFormatError(ValueError):
    """The segment cannot be interpreted as a SQLite WAL."""


def _checksum(data, seed1, seed2, big_endian):
    if len(data) % 8:
        raise WalFormatError('WAL checksum input is not a whole number of 8-byte words')
    first, second = seed1, seed2
    order = '>II' if big_endian else '<II'
    for offset in range(0, len(data), 8):
        left, right = struct.unpack_from(order, data, offset)
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


def parse(segment):
    """Describe a WAL segment without modifying it.

    ``frames`` lists only checksum-valid frames, in WAL order.  Parsing stops
    at the first invalid frame, matching SQLite's recovery rule.  ``state`` is
    ``empty`` (zero bytes), ``header-only`` (a valid header but no usable
    frame) or ``wal``.  ``has_uncommitted_tail`` is true only when a valid
    frame follows the last commit frame.
    """
    segment = bytes(segment)
    info = {
        'bytes': len(segment), 'state': 'empty', 'page_size': None,
        'frame_size': None, 'magic': None, 'big_endian_checksums': None,
        'checkpoint_sequence': None, 'salt': None, 'header_valid': None,
        'frames': [], 'valid_frames': 0, 'commit_frames': [], 'last_commit': None,
        'has_uncommitted_tail': False, 'trailing_bytes': 0, 'first_invalid': None,
    }
    if not segment:
        return info
    if len(segment) < 32:
        raise WalFormatError('WAL segment is shorter than its 32-byte header')
    magic, version, page_size, sequence, salt1, salt2, c1, c2 = struct.unpack('>IIIIIIII', segment[:32])
    if magic not in (MAGIC_LITTLE, MAGIC_BIG):
        raise WalFormatError(f'Unknown WAL magic 0x{magic:08x}')
    if version != WAL_VERSION:
        raise WalFormatError(f'Unsupported WAL version {version}')
    if page_size not in PAGE_SIZES:
        raise WalFormatError(f'Unsupported WAL page size {page_size}')
    big_endian = magic == MAGIC_BIG
    if _checksum(segment[:24], 0, 0, big_endian) != (c1, c2):
        raise WalFormatError('WAL header checksum does not match')
    info.update(page_size=page_size, frame_size=24 + page_size, magic=magic,
                big_endian_checksums=big_endian, checkpoint_sequence=sequence,
                salt=[salt1, salt2], header_valid=True)
    running = (c1, c2)
    offset = 32
    index = 0
    while offset + 24 + page_size <= len(segment):
        page, database_pages, fs1, fs2, fc1, fc2 = struct.unpack('>IIIIII', segment[offset:offset + 24])
        payload = segment[offset + 24:offset + 24 + page_size]
        running = _checksum(segment[offset:offset + 8], running[0], running[1], big_endian)
        running = _checksum(payload, running[0], running[1], big_endian)
        valid = (fs1, fs2) == (salt1, salt2) and (fc1, fc2) == running and 1 <= page <= MAX_PAGE
        if not valid:
            info['first_invalid'] = {'index': index, 'page': page,
                                     'salt_matches': (fs1, fs2) == (salt1, salt2),
                                     'checksum_matches': (fc1, fc2) == running}
            break
        if database_pages and database_pages * page_size > MAX_IMAGE_BYTES:
            raise WalFormatError('WAL commit image exceeds the image budget')
        info['frames'].append({'index': index, 'page': page, 'offset': offset,
                               'commit': bool(database_pages),
                               'database_pages': database_pages})
        if database_pages:
            info['commit_frames'].append(index)
            info['last_commit'] = index
        index += 1
        offset += 24 + page_size
    info['valid_frames'] = index
    info['trailing_bytes'] = len(segment) - offset
    info['state'] = 'wal' if index else 'header-only'
    info['has_uncommitted_tail'] = index > (0 if info['last_commit'] is None
                                            else info['last_commit'] + 1)
    return info


def _frame_payload(segment, frame, page_size):
    start = frame['offset'] + 24
    return segment[start:start + page_size]


def apply(base, segment, *, committed=True):
    """Rebuild a database image from ``base`` and a WAL segment.

    With ``committed=True`` only frames through the last commit frame are
    applied -- exactly the image a fresh SQLite reader may see.  With
    ``committed=False`` every valid frame is applied, exposing the page image
    a writer was holding when the segment was copied; that image is explicitly
    *not* a committed reader view.

    Returns ``(image_bytes, info)``.  A segment with no valid frame leaves the
    base image unchanged in both modes.
    """
    segment = bytes(segment)
    info = parse(segment)
    if info['state'] != 'wal':
        return bytes(base), info
    page_size = info['page_size']
    limit = info['valid_frames'] if not committed else (
        0 if info['last_commit'] is None else info['last_commit'] + 1)
    image = bytearray(base)
    for frame in info['frames'][:limit]:
        start = (frame['page'] - 1) * page_size
        end = start + page_size
        if end > MAX_IMAGE_BYTES:
            raise WalFormatError('WAL frame address exceeds the image budget')
        if len(image) < end:
            image.extend(b'\x00' * (end - len(image)))
        image[start:end] = _frame_payload(segment, frame, page_size)
    if committed and info['last_commit'] is not None:
        pages = info['frames'][info['last_commit']]['database_pages']
        size = pages * page_size
        if not size:
            raise WalFormatError('Commit frame has a zero database size')
        if len(image) > size:
            del image[size:]
        elif len(image) < size:
            image.extend(b'\x00' * (size - len(image)))
    return bytes(image), info
