"""Native APIs used by Red's pinned Discord.py, shared with the fresh repair probe."""

from importlib import import_module

VOICE_REQUIREMENTS = {"PyNaCl": "PyNaCl>=1.5.0,<1.6", "davey": "davey>=0.1.6"}


class IncompatibleVoiceLibrary(RuntimeError):
    def __init__(self, issues):
        # Only fixed API names appear here; never include native exception text/values.
        self.details = ", ".join(issues[:3])
        if len(issues) > 3:
            self.details += f", and {len(issues) - 3} more"
        super().__init__(self.details)


def _positive_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


_DAVE_METHODS = (
    "encrypt_opus",
    "reinit",
    "reset",
    "get_serialized_key_package",
    "set_passthrough_mode",
    "set_external_sender",
    "process_proposals",
    "process_commit",
    "process_welcome",
)
_API_CHECKS = {
    "davey": {
        "DAVE_PROTOCOL_VERSION": _positive_integer,
        "DaveSession": lambda value: isinstance(value, type),
        "CommitWelcome": lambda value: isinstance(value, type),
        "ProposalsOperationType.append": lambda value: value is not None,
        "ProposalsOperationType.revoke": lambda value: value is not None,
        **{f"DaveSession.{name}": callable for name in _DAVE_METHODS},
        "DaveSession.ready": lambda value: value is not None,
        "DaveSession.voice_privacy_code": lambda value: value is not None,
        "CommitWelcome.commit": lambda value: value is not None,
        "CommitWelcome.welcome": lambda value: value is not None,
    },
    "PyNaCl": {
        "secret.Aead": callable,
        "secret.Aead.encrypt": callable,
        "secret.Aead.NONCE_SIZE": _positive_integer,
        "secret.SecretBox": callable,
        "secret.SecretBox.encrypt": callable,
        "secret.SecretBox.NONCE_SIZE": _positive_integer,
        "utils.random": callable,
    },
}


def validate_voice_library(package, module):
    """Reject importable but incomplete libraries before opening voice connections."""
    issues = []
    for path, valid in _API_CHECKS[package].items():
        value = module
        for name in path.split("."):
            value = getattr(value, name, None)
        if not valid(value):
            issues.append(path)
    if issues:
        raise IncompatibleVoiceLibrary(issues)


def import_voice_library(package, *, importer=import_module):
    module = importer("nacl" if package == "PyNaCl" else "davey")
    if package == "PyNaCl":
        importer("nacl.secret")
        importer("nacl.utils")
    validate_voice_library(package, module)
    return module


def voice_library_status(*, importer=import_voice_library):
    statuses = {}
    for package in VOICE_REQUIREMENTS:
        try:
            importer(package)
        except IncompatibleVoiceLibrary as exc:
            statuses[package] = f"Incompatible ({exc.details})"
        except (ImportError, OSError, RuntimeError) as exc:
            missing = isinstance(exc, ModuleNotFoundError) and exc.name == (
                "nacl" if package == "PyNaCl" else "davey"
            )
            statuses[package] = "Missing" if missing else f"Import failed ({type(exc).__name__})"
        else:
            statuses[package] = "Ready"
    return statuses
