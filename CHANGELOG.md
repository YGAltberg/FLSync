# Ändringar

## 0.9.0 – första versionen

Allt som behövs för att spela i samma FL Studio-projekt, var och en i sin egen FL:

- **Live-synk** av mixern, kanalracket, stegsekvensern, mönster, plugin-parametrar, tempo, play/stop och spellistans spår, mellan FL Studio 2025 och 2026.
- **Ctrl+S = synka projektet:** sparningar slås ihop med de andras (noter och klipp ett och ett, kanaler, mönster, mixerkanaler och spår var för sig) och öppnas i allas FL. Samples och plugin-samples skickas med.
- **Olika datorer:** plugins som saknas, olika FL-versioner (2024.2–2026), egna samples.
- **Robust:** en tyst anslutning märks inom 20 sekunder, ändringar under ett avbrott skickas efteråt, värden som startar om tar tillbaka sessionen med sina versioner, och FLSync hittar sin plats igen om det startas om medan FL är öppet.
- **Ett eget program:** FLSync.exe öppnar FLSync i ett eget fönster utan terminal. Installation, reparation och avinstallation sker i samma fönster.
- **Över internet:** inbjudningskoder, UPnP i routern, och Tailscale som reserv.

Begränsningar: se README, *Kända begränsningar*.
