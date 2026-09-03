"""Single import point for the pjsua2 extension so the rest of the code never imports it directly."""
try:
    from ai_phone.vendor import pjsua2 as pj  # noqa: F401
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "pjsua2 extension not built. Run native/pjsua2/build.sh and copy build/pjsua2.py and "
        "build/_pjsua2*.so into src/ai_phone/vendor/."
    ) from e
