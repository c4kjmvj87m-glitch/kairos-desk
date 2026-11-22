"""Maak de GitHub-versie van de pagina uit de pagina van de Claude-desk.

Gebruik: python3 tools/sync_page.py <claude-index.html> [uit.html]

Het uiterlijk en de code blijven exact gelijk. Er verandert alleen dit:
- in de <head> komen de app-tags (beginscherm-icoon, manifest) en kairos-shim.js,
  dat de database, de naam, het voorlezen en Vraag de desk van Claude vervangt;
- een paar zinnen die naar Claude verwijzen, krijgen de tekst die op GitHub klopt.
Ontbreekt een van die stukken (omdat de Claude-pagina veranderd is), dan stopt het script.
"""
import pathlib
import sys

HEAD_ANCHOR = '<meta name="claude:viewport-fit-sides" content="cover">'
HEAD_ADD = HEAD_ANCHOR + """
<meta name="theme-color" content="#040807">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="KAIROS">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" type="image/png" sizes="192x192" href="icons/icon-192.png">
<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">
<script src="kairos-shim.js"></script>"""

TEXTS = [
    ("Vragen stellen werkt alleen als je deze pagina in Claude opent, ingelogd.",
     "Vragen stellen werkt met je eigen Claude-sleutel. Bij je eerste vraag plak je hem één keer."),
    ('t1 = "Geen verbinding: open de app in Claude"', 't1 = "Geen verbinding: herlaad de pagina"'),
    ("Open deze app in Claude terwijl je bent ingelogd. De desk-data staat in de database van de app.",
     "Herlaad de pagina. De desk-data komt van GitHub."),
    ("Claude is niet beschikbaar in deze weergave. Open de app in Claude en sta het gebruik van Claude toe.",
     "Vraag de desk heeft je Claude-sleutel nodig (begint met sk-ant-). Stel je vraag opnieuw en plak hem als daarom gevraagd wordt."),
    ("Je sessie is verlopen. Log opnieuw in bij Claude en stel je vraag nog eens.",
     "Je Claude-sleutel werkt niet (meer). Maak een nieuwe in de Claude Console en plak hem bij je volgende vraag."),
    ("Vraag de desk werkt alleen als je deze app in Claude opent en bent ingelogd.",
     "Vraag de desk heeft je Claude-sleutel nodig."),
    ("Je logboek werkt alleen als je deze app in Claude opent en bent ingelogd.",
     "Je logboek kon niet laden op dit toestel."),
    ("Open deze app in Claude om de desk te laden.", "Herlaad de pagina om de desk te laden."),
]


def convert(src):
    out = src
    if out.count(HEAD_ANCHOR) != 1:
        raise SystemExit("ankerpunt in <head> niet gevonden: de Claude-pagina is veranderd")
    out = out.replace(HEAD_ANCHOR, HEAD_ADD)
    for old, new in TEXTS:
        n = out.count(old)
        if n != 1:
            raise SystemExit(f"tekst {n}x gevonden in plaats van 1x: {old[:60]}")
        out = out.replace(old, new)
    left = [w for w in ("in Claude opent", "open de app in Claude", "Open deze app in Claude") if w in out]
    if left:
        raise SystemExit(f"nog verwijzingen naar Claude: {left}")
    return out


if __name__ == "__main__":
    src = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    dst = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "index.html")
    dst.write_text(convert(src), encoding="utf-8")
    print(f"{dst}: {dst.stat().st_size} bytes (bron {len(src.encode())} bytes)")
