# Diagram SVGs + rendered PDFs

The three diagrams the README embeds live here as **hand-authored SVGs**
(`architecture.svg`, `agent-flow.svg`, `server-catalog.svg`) — part of the AWS
repo's visual family (shared Check Point gradient, palette, and node styles),
not Mermaid exports. The README shows each `.svg` inline and links it to the
matching `.pdf` (GitHub's PDF viewer gives zoom and works on a private repo).

Regenerate the PDFs whenever you edit an SVG, so the two never drift:

```
./render-pdfs.sh
```

That runs `rsvg-convert` (librsvg — `brew install librsvg` on macOS) over each
SVG. The browsable Mermaid sources in [../diagrams](../diagrams) are the
inline-on-GitHub versions of the same diagrams; they are not what the README
embeds.

Never commit screenshots that expose subscription ids, tenant ids, real
hostnames, endpoints, or tokens.
