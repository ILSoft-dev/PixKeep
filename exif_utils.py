"""
exif_utils.py
v2.0 - inspect + strip metadata via exiftool (no recompression, pixels untouched)

Changelog:
- v2.0: added inspect_metadata() so the bot can report what it found/removed
        (GPS, camera, capture date, software) before stripping.
- v1.0: strip_exif via `exiftool -all=`.
"""
import json
import subprocess


def inspect_metadata(path: str) -> dict:
    """
    Return a small summary of privacy-relevant metadata in a file.
    { has_gps: bool, camera: str|None, datetime: str|None,
      software: str|None, tag_count: int }
    """
    try:
        result = subprocess.run(
            ["exiftool", "-j", "-n", path],
            check=True, capture_output=True, text=True,
        )
        data = json.loads(result.stdout)[0]
    except Exception:
        return {"has_gps": False, "camera": None, "datetime": None,
                "software": None, "tag_count": 0}

    has_gps = any(k.startswith("GPS") for k in data)
    camera_parts = [data.get("Make"), data.get("Model")]
    camera = " ".join(str(p) for p in camera_parts if p) or None
    dt = data.get("DateTimeOriginal") or data.get("CreateDate")
    software = data.get("Software")
    # These are file-system / container facts, not privacy metadata:
    ignored = {"SourceFile", "ExifToolVersion", "FileName", "Directory",
               "FileSize", "FileType", "FileTypeExtension", "MIMEType",
               "ImageWidth", "ImageHeight", "FileModifyDate",
               "FileAccessDate", "FileInodeChangeDate", "FilePermissions"}
    tag_count = len([k for k in data if k not in ignored])

    return {"has_gps": has_gps, "camera": camera, "datetime": dt,
            "software": software, "tag_count": tag_count}


def strip_exif(input_path: str, output_path: str) -> None:
    """
    Strip ALL metadata (EXIF/IPTC/XMP) from input_path -> output_path.
    `exiftool -all=` removes only metadata; image pixel data is not
    recompressed or altered.
    """
    subprocess.run(
        ["exiftool", "-all=", "-o", output_path, input_path],
        check=True, capture_output=True,
    )
