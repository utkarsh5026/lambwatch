"""Which AI models are set up, and the switches that decide when they are used.

Everything lives in one file, ``<archive>/ai.json``, written by the ``lw ai``
commands rather than by hand. That is a deliberate split from ``config.yaml``:

* the config file is hand-edited, commented, and safe to paste into a bug
  report; an API key does not belong anywhere near it
* ``lw ai add`` and ``lw ai remove`` have to rewrite the model list, and
  rewriting a commented YAML file loses the comments (see
  ``cli._rewrite_watch_dirs`` for how much care the one setting that *is*
  rewritten there needs)
* the background watcher re-reads this file for every version it explains, so
  ``lw ai off`` takes effect immediately, with no ``lw restart``

The file is written readable by its owner only, the same way ``~/.netrc`` and
``~/.aws/credentials`` are. A key can also stay in an environment variable,
with only the variable's name saved — ``lw ai add anthropic --key-env
ANTHROPIC_API_KEY`` — at the cost that a background service started without
that variable in its environment cannot see it.

Nothing is required: with no file at all, :meth:`AISettings.load` returns
settings with no models, and every caller treats that as "AI is not set up",
which is a normal state rather than an error.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from ..utils import LOG, utc_now_iso

#: The file the settings live in, inside the archive root.
SETTINGS_FILENAME = "ai.json"

#: Bumped only if the shape of ``ai.json`` ever has to break. Reading is
#: tolerant of anything older — see :meth:`AISettings.from_dict`.
SETTINGS_SCHEMA = 1


@dataclass(frozen=True)
class ProviderInfo:
    """What the rest of the tool needs to know about one kind of AI service.

    ``key_envs`` are the environment variables its own tools read, in the order
    they are tried, so a key someone already exported for the vendor's SDK is
    found without being asked for. ``prompt_chars`` and ``timeout`` are the
    defaults for a model on this service: a local model gets a much smaller
    prompt, because Ollama and friends default to a context window of a few
    thousand tokens and silently drop what does not fit, and a much longer
    timeout, because a laptop CPU writes a few words a second.
    """

    label: str
    key_envs: tuple[str, ...]
    needs_key: bool
    default_model: str
    key_url: str
    blurb: str
    prompt_chars: int
    timeout: int
    suggestions: tuple[tuple[str, str], ...] = ()


#: Every service a model can come from, keyed by the name ``lw ai add`` takes.
#: ``suggestions`` are shown when the service cannot be asked for its own list;
#: when it can, the list it returns is shown instead, since a hardcoded model
#: name is the first thing in this file to go stale.
PROVIDERS: dict[str, ProviderInfo] = {
    "anthropic": ProviderInfo(
        label="Anthropic",
        key_envs=("ANTHROPIC_API_KEY",),
        needs_key=True,
        default_model="claude-sonnet-5",
        key_url="https://console.anthropic.com/settings/keys",
        blurb="Claude",
        prompt_chars=150_000,
        timeout=240,
        suggestions=(
            ("claude-sonnet-5", "recommended — careful and quick"),
            ("claude-opus-5-5", "the most thorough, and the most expensive"),
            ("claude-haiku-4-5-20251001", "the fastest and cheapest"),
        ),
    ),
    "openai": ProviderInfo(
        label="OpenAI",
        key_envs=("OPENAI_API_KEY",),
        needs_key=True,
        default_model="gpt-5-mini",
        key_url="https://platform.openai.com/api-keys",
        blurb="GPT",
        prompt_chars=150_000,
        timeout=240,
        suggestions=(
            ("gpt-5-mini", "quick and inexpensive"),
            ("gpt-5", "more thorough"),
            ("gpt-4.1", "no reasoning step, so the fastest to answer"),
        ),
    ),
    "azure": ProviderInfo(
        label="Azure OpenAI",
        key_envs=("AZURE_OPENAI_API_KEY",),
        needs_key=True,
        default_model="",
        key_url="https://portal.azure.com (your resource → Keys and Endpoint)",
        blurb="a deployment in your Azure OpenAI resource",
        # Azure deployments are commonly provisioned with a tokens-per-minute
        # quota low enough that one full-size prompt trips it; a smaller one
        # avoids a 429 that no amount of retrying fixes within the minute.
        prompt_chars=100_000,
        timeout=240,
    ),
    "local": ProviderInfo(
        label="Local model",
        key_envs=(),
        needs_key=False,
        default_model="",
        key_url="",
        blurb="Ollama, LM Studio, vLLM or any OpenAI-compatible server",
        prompt_chars=16_000,
        timeout=600,
    ),
}

#: What ``lw ai add`` accepts for each provider, so ``claude`` or ``ollama``
#: finds the right one rather than being refused over a naming choice.
PROVIDER_ALIASES: dict[str, str] = {
    "claude": "anthropic", "gpt": "openai", "chatgpt": "openai",
    "azure-openai": "azure", "azureopenai": "azure",
    "ollama": "local", "lmstudio": "local", "lm-studio": "local", "vllm": "local",
    "openai-compatible": "local", "compatible": "local",
}


def provider_key(name: str) -> str | None:
    """The canonical provider name for what someone typed: ``Claude`` → ``anthropic``.

    ``None`` when nothing matches, so the caller can list the real choices.
    """
    lowered = name.strip().lower()
    if lowered in PROVIDERS:
        return lowered
    return PROVIDER_ALIASES.get(lowered)


def mask_key(key: str) -> str:
    """An API key as something safe to print: ``sk-ant-api03-…9f2c``.

    Enough of the front to recognise which account a key belongs to, and the
    last four characters to tell two keys from the same account apart — the
    same shape the providers' own dashboards use. A short key is starred out
    entirely, since half of eight characters is most of a key.
    """
    key = key.strip()
    if not key:
        return ""
    if len(key) <= 12:
        return "•" * len(key)
    prefix = key[: min(10, len(key) // 3)]
    return f"{prefix}…{key[-4:]}"


@dataclass
class ModelEntry:
    """One model the user has set up: which service, which model, and how to reach it.

    ``name`` is what the user types (``lw explain orders --model quick``); it
    defaults to the model id itself. ``model`` is the id sent to the service —
    for Azure, the *deployment* name, because that is what an Azure request is
    addressed to. ``endpoint`` is needed for Azure and local servers and is an
    optional override for the other two (a corporate gateway, say).

    The key is either saved here (``api_key``) or read from an environment
    variable named in ``key_env``; see :meth:`resolved_key` for the order.
    """

    name: str
    provider: str
    model: str
    api_key: str = ""
    key_env: str = ""
    endpoint: str = ""
    api_version: str = ""
    added_at: str = ""

    @property
    def info(self) -> ProviderInfo:
        """The static facts about this entry's service."""
        return PROVIDERS.get(self.provider, PROVIDERS["local"])

    @property
    def label(self) -> str:
        """How this model is named in prose: ``claude-sonnet-5 (Anthropic)``."""
        return f"{self.model or self.name} ({self.info.label})"

    def resolved_key(self) -> str:
        """The API key to send, or an empty string when there is none.

        A saved key wins; then the variable this entry names; then the
        service's usual variables, so an entry saved without a key still works
        in a shell that exports one. Empty is a legitimate answer for a local
        server, which usually needs no key at all.
        """
        if self.api_key:
            return self.api_key
        if self.key_env:
            return os.environ.get(self.key_env, "")
        for name in self.info.key_envs:
            value = os.environ.get(name, "")
            if value:
                return value
        return ""

    def key_source(self) -> str:
        """Where the key comes from, for ``lw ai``: ``sk-ant-…9f2c (saved)``, ``$VAR (not set)``."""
        if self.api_key:
            return f"{mask_key(self.api_key)} (saved)"
        if self.key_env:
            if os.environ.get(self.key_env):
                return f"from ${self.key_env}"
            return f"${self.key_env} (not set)"
        for name in self.info.key_envs:
            if os.environ.get(name):
                return f"from ${name}"
        return "not needed" if not self.info.needs_key else "missing"

    def key_problem(self) -> str | None:
        """Why this entry cannot be used right now, or ``None`` when it can.

        Only a missing key is checked — whether the key *works* takes a request,
        which ``lw ai test`` makes and nothing else does behind the user's back.
        """
        if not self.info.needs_key or self.resolved_key():
            return None
        if self.key_env:
            return (f"its key is read from ${self.key_env}, which is not set here. "
                    f"`lw ai add {self.provider} --name {self.name}` saves a key instead")
        return f"no API key. `lw ai add {self.provider} --name {self.name}` adds one"

    def as_dict(self) -> dict[str, Any]:
        """This entry as the JSON object ``ai.json`` stores."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelEntry | None:
        """Read one entry back, or ``None`` when it is too damaged to use.

        Unknown keys are ignored and missing ones take their defaults, so a file
        written by a later release still loads here. An entry with no provider
        or no name is dropped rather than failing the whole file.
        """
        if not isinstance(data, dict):
            return None
        known = {f.name for f in fields(cls)}
        values = {k: str(v) for k, v in data.items() if k in known and v is not None}
        provider = provider_key(values.get("provider", ""))
        if provider is None or not values.get("name"):
            return None
        values["provider"] = provider
        values.setdefault("model", "")
        return cls(**values)  # type: ignore[arg-type]


class SettingsError(Exception):
    """``ai.json`` exists but cannot be read; the message says what to do."""


@dataclass
class AISettings:
    """Everything ``lw ai`` manages: the models, the default, and the switches.

    The switches, and what each one means to the person flipping it:

    ``enabled``
        The master switch. Off, nothing is ever sent anywhere and reports stop
        mentioning AI at all — ``lw ai off`` without having to delete a key.
    ``auto_explain``
        Explain each new version as the watcher archives it, so the report is
        already annotated when someone opens it. Off, explanations are written
        only when asked for with ``lw explain``.
    ``send_code``
        Whether the changed lines of first-party code are sent. Off, the model
        sees only the structure — files, dependencies, env vars, services,
        findings — which says less but keeps source code on the machine.

    ``timeout_seconds`` and ``max_prompt_kb`` of 0 mean "the service's own
    default" (see :class:`ProviderInfo`); anything else overrides it for every
    model.
    """

    enabled: bool = True
    auto_explain: bool = True
    send_code: bool = True
    default: str = ""
    timeout_seconds: int = 0
    max_retries: int = 4
    max_prompt_kb: int = 0
    models: list[ModelEntry] = field(default_factory=list)
    #: Where these settings were read from and will be written back to.
    path: Path | None = None
    #: Set when the file existed but could not be read, so the next save moves
    #: it aside instead of silently replacing whatever was in it.
    problem: str | None = None

    # -- reading and writing --------------------------------------------
    @classmethod
    def load(cls, root: Path) -> AISettings:
        """The settings stored under an archive root, or empty defaults.

        Never raises: a missing file is the normal "not set up" state, and a
        damaged one is reported through :attr:`problem` so ``lw`` and ``lw ai``
        can say so and name the fix, rather than every command failing over a
        feature most of them do not use.
        """
        path = Path(root) / SETTINGS_FILENAME
        if not path.exists():
            return cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.warning("could not read %s: %s", path, exc)
            return cls(path=path, problem=f"{path} could not be read ({exc})")
        settings = cls.from_dict(data if isinstance(data, dict) else {})
        settings.path = path
        return settings

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AISettings:
        """Build settings from the parsed file, defaulting whatever is absent.

        Values of the wrong type fall back to their defaults one at a time, so a
        hand edit that turns ``4`` into ``"four"`` costs that one setting, not
        every model the file holds.
        """
        defaults = cls()
        values: dict[str, Any] = {}
        for name in ("enabled", "auto_explain", "send_code"):
            value = data.get(name)
            values[name] = value if isinstance(value, bool) else getattr(defaults, name)
        for name in ("timeout_seconds", "max_retries", "max_prompt_kb"):
            value = data.get(name)
            ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
            values[name] = value if ok else getattr(defaults, name)
        values["default"] = str(data.get("default") or "")
        models = [ModelEntry.from_dict(m) for m in data.get("models") or [] if isinstance(m, dict)]
        values["models"] = [m for m in models if m is not None]
        return cls(**values)

    def as_dict(self) -> dict[str, Any]:
        """The settings as ``ai.json`` stores them, schema number first."""
        return {
            "schema": SETTINGS_SCHEMA,
            "enabled": self.enabled,
            "auto_explain": self.auto_explain,
            "send_code": self.send_code,
            "default": self.default,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "max_prompt_kb": self.max_prompt_kb,
            "models": [m.as_dict() for m in self.models],
        }

    def save(self) -> Path:
        """Write the settings back, readable only by their owner, in one step.

        The file is created with mode ``0600`` before anything is written into
        it, so there is no moment at which a key sits in a world-readable file,
        and it replaces the old one with a rename, so a crash mid-write leaves
        the previous settings rather than half of the new ones. A file that
        could not be read when these settings were loaded is kept beside it as
        ``ai.json.broken`` instead of being overwritten, since it may hold keys
        its owner wants back.
        """
        if self.path is None:
            raise SettingsError("these settings have nowhere to be saved")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.problem and self.path.exists():
            broken = self.path.with_name(self.path.name + ".broken")
            try:
                os.replace(self.path, broken)
            except OSError as exc:
                raise SettingsError(f"could not move the unreadable {self.path} aside: {exc}") from exc
            self.problem = None
        scratch = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        text = json.dumps(self.as_dict(), indent=2) + "\n"
        try:
            descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(scratch, self.path)
        except OSError as exc:
            raise SettingsError(f"could not write {self.path}: {exc}") from exc
        finally:
            scratch.unlink(missing_ok=True)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass  # Windows: the profile's own ACL already keeps it private
        return self.path

    # -- choosing a model -------------------------------------------------
    def find(self, name: str) -> ModelEntry | None:
        """The saved model a name refers to: exact, then any case, then a unique prefix.

        ``lw ai use sonnet`` finds ``claude-sonnet-5`` when it is the only saved
        model starting that way — matched against both the entry's name and its
        model id, since people remember whichever one they typed last.
        """
        wanted = name.strip().lower()
        if not wanted:
            return None
        for entry in self.models:
            if entry.name == name:
                return entry
        for entry in self.models:
            if entry.name.lower() == wanted or entry.model.lower() == wanted:
                return entry
        starts = [e for e in self.models
                  if e.name.lower().startswith(wanted) or e.model.lower().startswith(wanted)]
        if len(starts) == 1:
            return starts[0]
        contains = [e for e in self.models if wanted in e.name.lower() or wanted in e.model.lower()]
        return contains[0] if len(contains) == 1 else None

    def default_entry(self) -> ModelEntry | None:
        """The saved model used when none is named, or ``None`` with none saved.

        Falls back to the first saved model when ``default`` names one that has
        since been removed, rather than leaving the user with models and no way
        to use them without a ``lw ai use``.
        """
        chosen = self.find(self.default) if self.default else None
        return chosen or (self.models[0] if self.models else None)

    def detected_entries(self) -> list[ModelEntry]:
        """Models usable straight from the environment, with nothing saved.

        Someone with ``ANTHROPIC_API_KEY`` already exported can run ``lw
        explain`` before ever running ``lw ai add``. These are only ever used
        when asked for from a terminal: the background watcher explains with
        *saved* models alone, because sending code off the machine unasked is
        something a person has to have switched on, not something a stray
        variable does. Azure is not detected — its deployment name has no
        standard variable to come from.
        """
        found: list[ModelEntry] = []
        for provider in ("anthropic", "openai"):
            info = PROVIDERS[provider]
            for env in info.key_envs:
                if os.environ.get(env):
                    found.append(ModelEntry(name=info.default_model, provider=provider,
                                            model=info.default_model, key_env=env))
                    break
        return found

    def resolve(self, requested: str | None = None, *, allow_detected: bool = True) -> ModelEntry | None:
        """The model to use for one request, or ``None`` when there is nothing to use.

        In order: a saved model matching ``requested``; then ``requested`` as a
        model id on the default model's service (``--model claude-opus-5-5``
        borrows the saved Anthropic key without having to be added first); then
        the default; then, from a terminal, a key found in the environment.
        """
        if requested:
            entry = self.find(requested)
            if entry is not None:
                return entry
            base = self.default_entry()
            detected = self.detected_entries() if allow_detected else []
            base = base or (detected[0] if detected else None)
            if base is None:
                return None
            # For Azure the id is a deployment name, which is exactly what a
            # request there is addressed to, so the same substitution holds.
            return ModelEntry(**{**base.as_dict(), "name": requested, "model": requested})
        entry = self.default_entry()
        if entry is not None:
            return entry
        detected = self.detected_entries() if allow_detected else []
        return detected[0] if detected else None

    def auto_entry(self) -> ModelEntry | None:
        """The model the background watcher explains new versions with, if it should.

        ``None`` whenever it should not: AI switched off, automatic
        explanations switched off, nothing saved, or the default model's key
        missing. Environment-detected keys never count here; see
        :meth:`detected_entries` for why.
        """
        if not (self.enabled and self.auto_explain):
            return None
        entry = self.default_entry()
        if entry is None or entry.key_problem():
            return None
        return entry

    def add(self, entry: ModelEntry, make_default: bool = False) -> bool:
        """Save a model, replacing any saved under the same name. Returns whether it replaced one.

        Adding a model again under its old name is how a key is changed, so a
        replacement keeps the entry's place in the list rather than moving it to
        the end. The first model saved becomes the default whatever
        ``make_default`` says, because a default that names nothing is useless.
        """
        entry.added_at = entry.added_at or utc_now_iso()
        replaced = False
        for index, existing in enumerate(self.models):
            if existing.name == entry.name:
                self.models[index] = entry
                replaced = True
                break
        else:
            self.models.append(entry)
        if make_default or not self.default or self.find(self.default) is None:
            self.default = entry.name
        return replaced

    def remove(self, entry: ModelEntry) -> ModelEntry | None:
        """Forget a saved model and its key. Returns the model that is now the default.

        Removing the default hands the role to the first model left, so ``lw
        explain`` keeps working for anyone with a second model saved.
        """
        self.models = [m for m in self.models if m.name != entry.name]
        if self.default == entry.name:
            self.default = self.models[0].name if self.models else ""
        return self.default_entry()

    def unique_name(self, wanted: str, provider: str) -> str:
        """A name for a new model that will not replace a different one.

        ``claude-sonnet-5`` stays ``claude-sonnet-5`` when it is new or when the
        saved one of that name is the same service's (adding it again updates
        it). A clash with another service's model of the same name — two local
        servers both calling theirs ``llama3`` — gets the service in front.
        """
        wanted = wanted.strip() or provider
        existing = next((m for m in self.models if m.name == wanted), None)
        if existing is None or existing.provider == provider:
            return wanted
        candidate, counter = f"{provider}-{wanted}", 2
        while any(m.name == candidate for m in self.models):
            candidate, counter = f"{provider}-{wanted}-{counter}", counter + 1
        return candidate

    # -- per-request limits ---------------------------------------------
    def prompt_budget(self, entry: ModelEntry) -> int:
        """How many characters of prompt a request to this model may carry.

        About four characters to a token, so the cloud default of 150,000 is
        roughly 40,000 tokens: a small fraction of any current model's context
        window, and about a tenth of a US dollar per explanation on a mid-range
        model. ``max_prompt_kb`` overrides it for everyone.
        """
        if self.max_prompt_kb > 0:
            return self.max_prompt_kb * 1024
        return entry.info.prompt_chars

    def timeout(self, entry: ModelEntry) -> float:
        """Seconds to wait on a silent connection before giving up on that attempt."""
        return float(self.timeout_seconds or entry.info.timeout)
