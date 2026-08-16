"""Native Python bindings for starting and managing sing-box."""

from pathlib import Path as _Path

from . import _native as _native

if hasattr(_native, "_loadCronetLibrary"):
    _cronet_candidates = (
        _Path(__file__).with_name("libcronet.dll"),
        _Path(__file__).with_name("libcronet.so"),
    )
    _cronet_path = next(
        (candidate for candidate in _cronet_candidates if candidate.is_file()),
        None,
    )
    if _cronet_path is None:
        raise ImportError("the wheel-bundled Cronet runtime is missing")
    _native._loadCronetLibrary(str(_cronet_path.resolve()))

SingBox = _native.SingBox
SingBoxError = _native.SingBoxError
__version__ = _native.__version__
startFromJSON = _native.startFromJSON

__all__ = [
    "SingBox",
    "SingBoxError",
    "__version__",
    "startFromJSON",
]
