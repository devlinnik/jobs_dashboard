import struct
from pathlib import Path

import pytest

from dashboard.windows_icon import verify_icon_resources


ICO = Path(__file__).resolve().parents[1] / 'dashboard/assets/devlin-desk.ico'


def resources_from_ico():
    data = ICO.read_bytes()
    header = data[:6]
    count = struct.unpack_from('<HHH', header)[2]
    group = bytearray(header)
    resources = {}
    for index in range(count):
        entry = struct.unpack_from('<BBBBHHII', data, 6 + 16 * index)
        resource_id = index + 1
        group.extend(struct.pack('<BBBBHHIH', *entry[:7], resource_id))
        size, offset = entry[6:]
        resources[(3, resource_id)] = data[offset:offset + size]
    resources[(14, 1)] = bytes(group)
    return resources


def test_embedded_icon_matches_all_sizes_and_pixels():
    resources = resources_from_ico()
    verify_icon_resources(ICO, lambda kind, identifier: resources[(kind, identifier)])


def test_outdated_embedded_icon_is_rejected_even_when_ico_file_is_correct():
    resources = resources_from_ico()
    payload = resources[(3, 1)]
    resources[(3, 1)] = payload[:-1] + bytes([payload[-1] ^ 1])
    with pytest.raises(RuntimeError, match='outdated or different'):
        verify_icon_resources(ICO, lambda kind, identifier: resources[(kind, identifier)])


def test_missing_embedded_icon_size_is_rejected():
    resources = resources_from_ico()
    resources[(14, 1)] = struct.pack('<HHH', 0, 1, 1)
    with pytest.raises(RuntimeError, match='group does not match'):
        verify_icon_resources(ICO, lambda kind, identifier: resources[(kind, identifier)])
