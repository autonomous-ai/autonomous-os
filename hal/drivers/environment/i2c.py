"""Sensirion word CRC framing shared by environmental I2C drivers."""


def crc8(data: bytes) -> int:
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ (0x31 if crc & 0x80 else 0)) & 0xFF
    return crc


def decode_words(data: bytes, count: int, sensor: str = "SEN55") -> list[int]:
    if len(data) != count * 3:
        raise OSError(f"{sensor}: incomplete I2C response")
    words = []
    for offset in range(0, len(data), 3):
        pair = data[offset:offset + 2]
        if crc8(pair) != data[offset + 2]:
            raise OSError(f"{sensor}: CRC mismatch")
        words.append(int.from_bytes(pair, "big"))
    return words
