# zorin-clipboard-pling

Ett litet pling när urklippet ändras, och en bro till Zorin Connect så att du kan skicka eller hämta urklippet till telefonen. Körs från en terminal, utan Jarri.

Klassificeringsregeln (`text`, `json`, `url`, `claude`, `empty`) följer ClipboardBytes av TorMatzAndren (github.com/TorMatzAndren/ClipboardBytes). Ingen kod från det projektet används.

## Kom igång

1. Starta tjänsten i en terminal:

   ```
   python3 server.py
   ```

   Standardport är 8787. Den bevakar urklippet och spelar ett pling när det ändras.

2. Kontrollera att den svarar:

   ```
   curl -H "Host: 127.0.0.1:8787" http://127.0.0.1:8787/status
   ```

## Vad den kan

| Metod | Sökväg | Vad |
|---|---|---|
| GET | `/clipboard` | `{kind, bytes, lines, copiedAt, seq}`. Själva texten lämnar aldrig tjänsten. |
| GET | `/status` | Session, om bevakningen körs, pling, auto-push och Zorin-läget med enheter. |
| POST | `/pling` | Spelar plinget en gång, för att testa ljudet. |
| POST | `/push` | Body `{"deviceId": "..."}`. Zorin skickar datorns urklipp till telefonen. |
| POST | `/pull` | Body `{"deviceId": "..."}`. Zorin hämtar telefonens urklipp till datorn. |

Zorin Connect gör synken mellan mobil och dator själv. `/push` och `/pull` är manuella knappar utöver den.

## Krav

- Wayland med `wl-paste`, eller X11 med `xclip` eller `xsel`.
- Ljud: `canberra-gtk-play`. Plinget är det egna, samma som Jarri använde. Det spelas aldrig från systemets ljudtema.
- För Zorin: Zorin Connect igång på datorn. Utan den svarar `/status` med `"available": false`, och övriga funktioner fungerar ändå.

## Inställningar

| Variabel | Standard | Vad |
|---|---|---|
| `CLIPBOARD_PORT` | 8787 | port |
| `CLIPBOARD_PLING` | 1 | 0 = inget ljud |
| `CLIPBOARD_AUTOPUSH` | 0 | 1 = skicka urklippet till alla anslutna telefoner vid varje ändring. Av som standard, eftersom kopierad text kan vara känslig. |
| `CLIPBOARD_POLL_S` | 0.5 | hur ofta urklippet kollas, i sekunder |

## Tester

```
python3 -m unittest discover -v
```

## Licens

Copyright (c) 2026 Kenny Sjöstedt.

Licensierad under GNU Affero General Public License v3.0 (se [LICENSE](LICENSE)).

Kommersiell licens finns hos upphovsmannen för användning som AGPL inte tillåter. Kontakta upphovsmannen via GitHub.
