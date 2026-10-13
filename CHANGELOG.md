# Ändringar

## 0.13.0 – nya instrument live

- **Lägger någon in ett instrument (en VST eller FL:s egna, t.ex. Serum, FLEX, Sytrus) dyker det upp hos de andra inom några sekunder**, med exakt samma inställningar, utan att projektet laddas om och även mitt i en uppspelning. FL sparar själv en sekund efter att instrumentet kom, och FLSync ger det till de andras FL som ett plugin-preset som FL lägger in sist i kanalracket. Sedan synkas dess rattar live. Testat i FL 2026.1.7 med FLEX och Serum 2: alla parametrar blev lika.
- Det gäller instrument som läggs in sist i kanalracket. Ett instrument mitt i kanalracket, effekter i mixern, samples och klipp kommer som förut: de laddas in när de andra har stoppat uppspelningen.
- `.fst`-filer öppnas bara i det FL som kör (Windows kunde ha dem kopplade till en annan FL-version).

## 0.12.0 – smidigare

- **Ctrl+S direkt efter att du öppnat ett projekt delas.** FLSync hittar projektfilen via namnet i FL:s titelrad, i FL:s projektmapp, om det bara finns en fil med det namnet. Förut syntes en sparning först när FL hade något osparat, och automatisk sparning kom inte igång förrän dess.
- **Skyddsnät för innehåll:** har FL osparade ändringar som FLSync inte kunde se vad de var, sparar FL ändå när uppspelningen står still en stund (högst var 20:e sekund). Det delas bara om något nytt faktiskt finns.
- **FLSync-fönstret säger när en ny version finns** (det frågar GitHub en gång när det startar) och öppnar sidan att ladda ner den från.
- **Deltagarna visar vilken FLSync de har**, och fönstret varnar om någon har en annan version än du.
- **Spara och hämta automatiskt** går att stänga av i fönstret, och där står vad som händer just nu (t.ex. att någons ändringar hämtas in när du stoppar).

## 0.11.0 – innehåll utan Ctrl+S

- **FL sparar av sig själv** (FL 2026.1.7 eller senare): har du lagt till kanaler, plugins, samples, klipp eller noter, alltså sådant som inte kan synkas live, sparar FL några sekunder efter att du slutat ändra, och det delas som när du trycker Ctrl+S. En sparning med bara sådant som redan synkas live delas inte.
- **De andras ändringar laddas in av sig själva:** när uppspelningen är stoppad och du inte rör något räknar hint-raden ner, din FL sparar dina egna ändringar och öppnar det sammanslagna projektet. Mitt i en uppspelning laddas aldrig något.
- **Stora sessioner ansluter på en gång:** förut kunde den som anslöt till en lång session (mycket att skicka) börja om igen och igen. Bryggan skickar nu i jämn takt till FL.
- Testat live i FL 2026.1.7: FL sparar på begäran utan dialogrutor, och skriptets egna ändringar hamnar i ångra-historiken (FLSync räknar dem inte som innehåll). Gophers modul för att lägga till kanaler och effekter går inte att använda från ett MIDI-script, så nya plugins kan inte dyka upp live hos de andra.

## 0.10.0 – stabilare session

Efter det första riktiga testet på två datorer:

- **FL som hänger stannar kvar.** När FL laddar ett plugin eller ett projekt, sparar eller visar en dialogruta svarar det inte på några sekunder. Förut lämnade FLSync då sessionen, och ibland kom FL aldrig tillbaka in utan att märka det. Nu stannar FL kvar så länge fönstret finns, och det de andra ändrat under tiden kommer fram när FL svarar igen.
- **Ett nytt plugin pausar inte allt.** Ett nytt instrument pausar bara kanalerna och en ny effekt bara mixern. Tempo, play/stop och spellistans spår synkar alltid. Det som pausats kommer tillbaka när ändringen sparas och delas, som förut.
- **Ett plugin som laddar kan inte stänga av FLSync.** Krånglar en del av FL en stund fortsätter resten, och felet skrivs i loggen i stället för att räknas mot FLSync.
- **Byta projekt:** sparar du ett projekt som inte är sessionens får de andra veta det. Tryck Ctrl+S igen inom 15 sekunder, så blir det allas projekt och öppnas hos de andra. Deras gamla projekt slås aldrig ihop med det nya.
- **Swing synkas i FL 2026** (funktionen heter `getSwing` där).
- **FL:s egna meddelanden hamnar i FLSync-fönstrets logg:** när FL var upptaget och hur länge, fel och pauser, och vilka funktioner FL erbjuder skript.

## 0.9.0 – första versionen

Allt som behövs för att spela i samma FL Studio-projekt, var och en i sin egen FL:

- **Live-synk** av mixern, kanalracket, stegsekvensern, mönster, plugin-parametrar, tempo, play/stop och spellistans spår, mellan FL Studio 2025 och 2026.
- **Ctrl+S = synka projektet:** sparningar slås ihop med de andras (noter och klipp ett och ett, kanaler, mönster, mixerkanaler och spår var för sig) och öppnas i allas FL. Samples och plugin-samples skickas med.
- **Olika datorer:** plugins som saknas, olika FL-versioner (2024.2–2026), egna samples.
- **Robust:** en tyst anslutning märks inom 20 sekunder, ändringar under ett avbrott skickas efteråt, värden som startar om tar tillbaka sessionen med sina versioner, och FLSync hittar sin plats igen om det startas om medan FL är öppet.
- **Ett eget program:** FLSync.exe öppnar FLSync i ett eget fönster utan terminal. Installation, reparation och avinstallation sker i samma fönster.
- **Över internet:** inbjudningskoder, UPnP i routern, och Tailscale som reserv.

Begränsningar: se README, *Kända begränsningar*.
