"""Plain-English explanations of what changed between two archived versions.

Everything here sits on top of the diff engine rather than beside it: the
comparison in :mod:`lambda_watcher.diffing` already decides *what* changed, and
a language model's only job is to say what that means for someone about to
deploy it. So nothing in this package ever reads a zip, and nothing it produces
goes into ``index.db`` — explanations live beside the version they describe,
where ``lw reindex`` never has to know about them.

The modules, in the order a request flows through them:

* :mod:`.settings` — which models the user has set up, their keys, and the
  switches (``ai.json`` in the archive root, managed by ``lw ai``)
* :mod:`.prompt` — a :class:`~lambda_watcher.diffing.compare.VersionDiff` as
  prompt text, inside a size budget and with credentials redacted
* :mod:`.providers` — Anthropic, OpenAI, Azure OpenAI and local servers over
  plain HTTPS, with retries
* :mod:`.explanation` — the model's answer parsed into something both renderers
  can draw, and the file it is saved in
* :mod:`.run` — the pieces above as one call, plus the background worker the
  watcher hands new versions to

Deliberately empty of imports: :mod:`lambda_watcher.diffing.build` reads saved
explanations while the diffing package is still initialising, and an eager
import of :mod:`.prompt` from here would import the diffing package back.
"""
