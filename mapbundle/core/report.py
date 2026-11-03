"""Collects what happened during an export and renders it for the user."""

from __future__ import annotations

import html
from dataclasses import dataclass, field


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


@dataclass
class Report:
    output_path: str = ""
    project_uri: str = ""
    success: bool = False
    layers: list = field(default_factory=list)          # (layer name, description)
    embedded_files: list = field(default_factory=list)  # (path, size)
    fonts_bundled: list = field(default_factory=list)   # (family, files, licence)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    output_size: int = 0
    assets_zip: str = ""
    assets_size: int = 0

    def warn(self, message: str):
        self.warnings.append(message)

    def error(self, message: str):
        self.errors.append(message)

    def to_html(self) -> str:
        e = html.escape
        parts = []
        if self.success:
            parts.append(f"<h3>MapBundle exported</h3><p><b>{e(self.output_path)}</b> "
                         f"({human_size(self.output_size)})</p>")
            if self.assets_zip:
                parts.append(f"<p>Assets: {e(self.assets_zip)} ({human_size(self.assets_size)})</p>")
        else:
            parts.append("<h3>Export failed</h3>")
        if self.errors:
            parts.append("<h4>Errors</h4><ul>" + "".join(
                f"<li style='color:#b00020'>{e(m)}</li>" for m in self.errors) + "</ul>")
        if self.warnings:
            parts.append("<h4>Check these</h4><ul>" + "".join(
                f"<li>{e(m)}</li>" for m in self.warnings) + "</ul>")
        if self.layers:
            parts.append("<h4>Layers</h4><ul>" + "".join(
                f"<li><b>{e(n)}</b>: {e(d)}</li>" for n, d in self.layers) + "</ul>")
        if self.embedded_files:
            total = sum(s for _, s in self.embedded_files)
            parts.append(f"<h4>Embedded images ({len(self.embedded_files)}, {human_size(total)})</h4><ul>"
                         + "".join(f"<li>{e(p)} ({human_size(s)})</li>" for p, s in self.embedded_files)
                         + "</ul>")
        if self.fonts_bundled:
            parts.append("<h4>Bundled fonts</h4><ul>" + "".join(
                f"<li><b>{e(fam)}</b>: {n} file(s), {e(lic)}</li>" for fam, n, lic in self.fonts_bundled)
                + "</ul>")
        if self.success:
            parts.append(
                "<p>Recipients with the MapBundle plugin: <i>Plugins → MapBundle → Add MapBundle "
                "to Project…</i> (installs bundled fonts).<br>Without the plugin: "
                "<i>Project → Open From → GeoPackage</i>.</p>")
        return "".join(parts)

    def to_text(self) -> str:
        lines = [("OK: " if self.success else "FAILED: ") + self.output_path]
        lines += ["ERROR: " + m for m in self.errors]
        lines += ["WARNING: " + m for m in self.warnings]
        lines += [f"LAYER: {n}: {d}" for n, d in self.layers]
        lines += [f"EMBEDDED: {p} ({s} B)" for p, s in self.embedded_files]
        lines += [f"FONT: {f} ({n} files, {lic})" for f, n, lic in self.fonts_bundled]
        if self.assets_zip:
            lines.append(f"ASSETS: {self.assets_zip} ({self.assets_size} B)")
        return "\n".join(lines)
