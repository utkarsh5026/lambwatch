"""File-type icons for the report's file list.

Every row of a diff already spells out its path, so an icon that only repeated
the extension would be decoration. What it can add is *shape*: a scroll through
sixty changed files should show at a glance where the code is, where the
configuration is, and which one of them is a ``.env``.

So the icons are eight glyph families rather than sixty logos — nothing is
borrowed artwork, nothing needs a licence, and a language nobody thought of
still gets a sensible mark. The language it came from survives as colour.

The glyphs are drawn once into a hidden ``<symbol>`` sprite that the document
carries at the top of its body; each row then costs one ``<use>``. The colours
are emitted as one small stylesheet from the same table, so a row costs no
inline style either — which matters when a vendored diff runs to thousands of
files.
"""

from __future__ import annotations

from pathlib import PurePosixPath

# 16×16, stroked rather than filled so a single colour drives the whole glyph.
GLYPHS: dict[str, str] = {
    "code":     '<path d="M6.2 4.4 2.6 8l3.6 3.6"/><path d="M9.8 4.4 13.4 8l-3.6 3.6"/>',
    "markup":   '<path d="M5.4 3.6 2 8l3.4 4.4"/><path d="M10.6 3.6 14 8l-3.4 4.4"/>'
                '<path d="M9.4 2.6 6.6 13.4"/>',
    "data":     '<path d="M6.6 2.8c-1.4 0-2 .7-2 1.9v1.4c0 1-.6 1.6-1.6 1.9 1 .3 1.6.9 1.6 1.9v1.4'
                'c0 1.2.6 1.9 2 1.9"/>'
                '<path d="M9.4 2.8c1.4 0 2 .7 2 1.9v1.4c0 1 .6 1.6 1.6 1.9-1 .3-1.6.9-1.6 1.9v1.4'
                'c0 1.2-.6 1.9-2 1.9"/>',
    "doc":      '<path d="M3.2 4.2h9.6"/><path d="M3.2 8h9.6"/><path d="M3.2 11.8h5.8"/>',
    "terminal": '<rect x="2.2" y="3.2" width="11.6" height="9.6" rx="2"/>'
                '<path d="M4.9 6.9 7 9l-2.1 2.1"/><path d="M8.7 11.1h2.8"/>',
    "style":    '<path d="M8 2.4c2.6 2.7 4 4.7 4 6.3a4 4 0 0 1-8 0c0-1.6 1.4-3.6 4-6.3z"/>',
    "lock":     '<rect x="3.2" y="7" width="9.6" height="6.2" rx="1.6"/>'
                '<path d="M5.6 7V5.4a2.4 2.4 0 0 1 4.8 0V7"/>',
    "image":    '<rect x="2.4" y="3.4" width="11.2" height="9.2" rx="1.8"/>'
                '<circle cx="6" cy="6.9" r="1.1"/><path d="m3 11.8 3.2-3 2.4 2.3 2.2-1.8 2.8 2.5"/>',
    "binary":   '<path d="M8 2.4 13.6 5.4v5.2L8 13.6 2.4 10.6V5.4z"/>'
                '<path d="M2.4 5.4 8 8.4l5.6-3"/><path d="M8 8.4v5.2"/>',
}

# Key -> (glyph, colour). The keys are the language labels `utils.language_for`
# produces plus the two below that no label can express, and one mid-tone colour
# each, because the report is read in both themes and the icon sits on the same
# panel in either one.
ICONS: dict[str, tuple[str, str]] = {
    "python":     ("code",     "#4b8bbe"),
    "javascript": ("code",     "#c9a227"),
    "typescript": ("code",     "#3178c6"),
    "java":       ("code",     "#c26a4a"),
    "kotlin":     ("code",     "#9a6ef0"),
    "go":         ("code",     "#23a3c4"),
    "ruby":       ("code",     "#cc4b4b"),
    "rust":       ("code",     "#c8763c"),
    "csharp":     ("code",     "#8a63c4"),
    "php":        ("code",     "#6e7fbc"),
    "scala":      ("code",     "#c4574a"),
    "swift":      ("code",     "#e0713d"),
    "c":          ("code",     "#6d8bbd"),
    "cpp":        ("code",     "#8a7fc4"),
    "sql":        ("code",     "#4a9aa8"),
    "shell":      ("terminal", "#6ea84f"),
    "powershell": ("terminal", "#3f7fd0"),
    "dockerfile": ("terminal", "#2f8fd0"),
    "makefile":   ("terminal", "#8a8f98"),
    "json":       ("data",     "#b58900"),
    "yaml":       ("data",     "#9068c0"),
    "toml":       ("data",     "#a5713d"),
    "ini":        ("data",     "#8a8f98"),
    "html":       ("markup",   "#e06c50"),
    "xml":        ("markup",   "#d4713f"),
    "css":        ("style",    "#3c9ad9"),
    "markdown":   ("doc",      "#7a8290"),
    "csv":        ("doc",      "#5f9e6e"),
    "text":       ("doc",      "#8b8f97"),
    "dotenv":     ("lock",     "#b58900"),
    "binary":     ("binary",   "#8b8f97"),
    # Two keys no language label produces. An image is indexed as `binary`,
    # the same label a shared object gets; and a file that carries credentials
    # is worth a lock whatever syntax happens to be inside it.
    "image":      ("image",    "#a06fc0"),
    "_other":     ("doc",      "#8b8f97"),
}

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp"}
SECRET_NAMES = ("credentials", "id_rsa", ".netrc", ".pem", ".key", ".p12", ".pfx")


def icon_key(path: str, lang: str) -> str:
    """Which entry of ``ICONS`` a file gets — what the path says first, then its language.

    ``.env`` files need no rule here: they reach this point already labelled
    ``dotenv`` by `highlight.language_of`, which is where reading a name for a
    language belongs.
    """
    name = PurePosixPath(path).name.lower()
    if PurePosixPath(name).suffix in IMAGE_SUFFIXES:
        return "image"
    if name.startswith(SECRET_NAMES) or name.endswith(SECRET_NAMES):
        return "dotenv"
    return lang if lang in ICONS else "_other"


def file_icon(path: str, lang: str) -> str:
    """A decorative icon for one file: the path beside it already names it."""
    key = icon_key(path, lang)
    glyph = ICONS[key][0]
    return (
        f'<svg class="fic fic-{key}" viewBox="0 0 16 16" aria-hidden="true">'
        f'<use href="#g-{glyph}"/></svg>'
    )


def sprite() -> str:
    """The hidden glyph sheet every ``file_icon`` points at, emitted once."""
    symbols = "".join(
        f'<symbol id="g-{name}" viewBox="0 0 16 16">{body}</symbol>' for name, body in GLYPHS.items()
    )
    return f'<svg class="sprite" aria-hidden="true">{symbols}</svg>'


def css() -> str:
    """One colour rule per key, generated from the table above."""
    base = [
        "svg.sprite { display: none; }",
        ".fic { width: 16px; height: 16px; flex: 0 0 auto; fill: none; stroke: currentColor;",
        "  stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }",
    ]
    rules = [f".fic-{key} {{ color: {colour}; }}" for key, (_, colour) in ICONS.items()]
    return "\n".join(base + rules) + "\n"
