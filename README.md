# FLSync – FL Studio tillsammans

Spela in och producera tillsammans i FL Studio, var och en i sin egen FL. Drar någon i en fader, vrider på ett plugin eller klickar i stegsekvensern, så händer samma sak hos de andra direkt. Lägger någon till kanaler, plugins, samples, noter eller klipp, så sparar FL det av sig själv och ändringarna slås ihop med de andras: alla får samma projekt. Alla hör ljudet från sin egen FL, i full kvalitet och utan fördröjning i ljudet.

**Du behöver inte göra något särskilt:** med FL 2026.1.7 eller senare sparar och hämtar FLSync själv. **Ctrl+S i FL = synka projektet nu** fungerar alltid (och behövs i äldre FL).

Fungerar på Windows med FL Studio 2025 och 2026, i samma nätverk eller över internet.

**Ladda ner:** [FLSync.zip](https://github.com/YGAltberg/FLSync/releases/latest/download/FLSync.zip) (den senaste versionen; alla versioner och vad som är nytt finns under [Releases](https://github.com/YGAltberg/FLSync/releases)). Packa upp den och dubbelklicka på FLSync.exe, se [Kom igång](#kom-igång).

## Kom igång

Det här gör alla som är med, en gång:

1. **Packa upp FLSync** ([`FLSync.zip`](https://github.com/YGAltberg/FLSync/releases/latest/download/FLSync.zip), eller den du har fått av någon som redan har FLSync): högerklicka på den och välj *Extrahera alla*. Dubbelklicka på **`FLSync.exe`** i den uppackade mappen. Säger Windows att det skyddade datorn: välj *Mer information > Kör ändå*.
2. **Klicka *Installera*** i FLSync-fönstret. Är FL Studio igång väntar installationen tills du har sparat och stängt det. Installationen:
   - installerar [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) om det saknas (FLSync frågar först, och Windows ber om lov) och skapar MIDI-porten `FLSync`, som FL och FLSync pratar genom;
   - installerar FLSync-scriptet i FL och två script för noter i pianorullen;
   - ställer in FL:s MIDI-inställningar åt dig;
   - lägger genvägen **FLSync** på skrivbordet och i Start-menyn, och **Reparera FLSync** och **Avinstallera FLSync** i Start-menyn.

   Du behöver inte installera Python: FLSync använder den som följer med FL. Ingen terminal öppnas: allt sker i FLSync-fönstret. Mappen du packade upp kan du ta bort efteråt. **Ny version av FLSync:** FLSync-fönstret säger till när det finns en (*Ladda ner*). Gör samma sak med den nya zip-filen; det uppdaterar det du har (du behöver inte avinstallera först). Kör alla i sessionen samma version: fönstret varnar annars.

Sedan, varje gång ni spelar:

3. **Starta FL Studio** och **dubbelklicka på FLSync** på skrivbordet. FLSync öppnas i ett eget fönster.
4. **En av er klickar *Starta session*** och skickar inbjudan (*Kopiera inbjudan*, en kod som börjar med `FLS1-`) till de andra, t.ex. i en chatt. **De andra klistrar in koden och klickar *Gå med*.**
5. **Den som har projektet** öppnar det i FL och sparar (Ctrl+S). De andra får det öppnat i sin FL av sig själv (har någon osparade ändringar i FL klickar hen ***Öppna senaste versionen*** i FLSync-fönstret i stället). Sedan synkas allt ni gör, och **Ctrl+S synkar projektet**.

Stänger du FLSync-fönstret avslutas FLSync. Gör du det (eller stänger datorn) mitt i en session går FLSync med i samma session igen nästa gång du startar det. *Lämna sessionen* avslutar den på riktigt.

**Över internet:** FLSync ber routern öppna porten åt den som startar sessionen (UPnP), och inbjudan innehåller både den lokala och den publika adressen. Går det inte, säger FLSync det; öppna då TCP-port 31415 i routern mot värdens dator, eller använd ett VPN som [Tailscale](https://tailscale.com) (FLSync hittar Tailscale-adressen själv). Sessionen har ett lösenord som ingår i inbjudan.

## Vad synkas

**Live, medan ni jobbar:**

| Del av FL | Vad |
|---|---|
| **Mixern** | volym, pan, mute, namn, färg, stereobredd, EQ (gain, frekvens och bredd per band), effekter på/av per spår, mixnivån för varje effektplats, routing (vilka spår som skickar till vilka, och hur mycket) |
| **Kanalracket** | volym, pan, tonhöjd, mute, namn, färg och vilket mixerspår kanalen går till |
| **Stegsekvensern** | stegen i varje mönster, och varje stegs parametrar (tonhöjd, velocity, release, fininställning, pan, mod X/Y, förskjutning) |
| **Mönster** | namn och färg |
| **Plugin-parametrar** | alla parametrar i det plugin vars fönster du har framme: instrument i kanalerna och effekter i mixern |
| **Låten** | tempo, taktartens täljare, swing (FL 2026), mönster-/låtläge, play och stop (alla startar från samma ställe) |
| **Spellistan** | spårens namn, färg och mute |
| **Närvaro** | FL:s hint-rad visar vem som ändrar vad, t.ex. `Anna: Insert 3 volym`, och när någon ansluter eller lämnar |

**Nya instrument, inom några sekunder:** lägger någon in ett instrument (en VST som Serum, eller FL:s egna som FLEX och Sytrus) sist i kanalracket, så dyker det upp hos de andra med samma inställningar, utan att projektet laddas om, även mitt i en uppspelning. Sedan synkas dess rattar live. (FL 2026.1.7 eller senare. Pluginet måste vara installerat hos de andra.)

**Av sig själv, några sekunder efter att du slutat ändra (eller när du sparar):** hela projektet, alltså nya och borttagna kanaler och plugins med sina inställningar, samples, noter, klipp i spellistan och automation. FL sparar själv när något sådant har ändrats och du inte har rört något på två sekunder (FL 2026.1.7 eller senare; i äldre FL trycker du Ctrl+S). FLSync skickar projektfilen och de samples som behövs, även samples inuti plugins som FPC och DirectWave. Hos de andra laddas det in när de har stoppat uppspelningen, efter en nedräkning i hint-raden. En sparning med bara sådant som redan synkas live delas inte. FL sparar då i din projektfil, som när du trycker Ctrl+S (tidigare versioner ligger i mappen `Versioner`). Vill du hellre bestämma själv: bocka ur *Spara och hämta automatiskt* i FLSync-fönstret. FL:s script-API kan inte ändra sådant live, så det går inte fortare än så. Se [Spara och slå ihop](#spara-och-slå-ihop).

**Noter på begäran:** i pianorullen kan du skicka ett mönsters noter till de andra, och hämta deras, utan att någon behöver spara eller öppna något. Se [Noter](#noter).

## Spara och slå ihop

Varje gång du sparar jämför FLSync tre versioner: den du utgick från, den du just sparade, och den senaste som någon annan har delat.

- **Ingen annan har sparat sedan du öppnade projektet:** din version delas som den är.
- **Någon annan har sparat:** FLSync slår ihop versionerna, delar resultatet och öppnar det i din FL (när du inte spelar; annars när du stoppar). Ändringar på olika ställen följer med båda: noter not för not (två kan skriva i samma mönster), klipp i spellistan klipp för klipp, kanaler, mönster, mixerkanaler och spår var för sig. Har ni ändrat *samma* sak behålls din ändring, och FLSync säger var.
- **Du hade inget nytt:** inget delas, du får den senaste versionen öppnad.
- **De andra** ser i hint-raden `Anna har sparat, hämtas in när du stoppar`. När deras uppspelning är stoppad och de inte rör något räknar hint-raden ner (`hämtar Annas ändringar om 3 s`), deras FL sparar deras egna ändringar och öppnar det sammanslagna projektet. Ändrar de något under nedräkningen börjar den om. (I FL äldre än 2026.1.7: `tryck Ctrl+S för att få med ändringarna`.)
- **Går det inte att slå ihop** (ni har båda lagt till kanaler, eller sparat i olika FL-versioner, t.ex. 2025 och 2026) ersätter den senaste sparningen den förra, och båda får veta det. Den ersatta versionen ligger kvar i mappen `Versioner` bredvid projektet.

FLSync skriver aldrig över dina egna filer. Sessionens projekt ligger i `Dokument\Image-Line\FL Studio\Projects\FLSync\<namn>\`, och där sparar FL sedan. Varje version som ersätts där kopieras först till `Versioner` (de 20 senaste sparas). Rattar, steg och plugin-inställningar är redan lika hos alla, eftersom de synkas live.

Ett projekt som inte hör till sessionen (du öppnade ett annat projekt) delas inte när andra är med, och de andra får veta att du sparade ett annat projekt. **Tryck Ctrl+S igen inom 15 sekunder** (eller klicka **Dela mitt projekt** i FLSync-fönstret), så blir ditt projekt sessionens: de andras FL öppnar det, och sparar de sitt gamla projekt öppnas ditt i stället för att de slås ihop.

## Om anslutningen bryts

- **Internet försvinner en stund:** FLSync märker en tyst anslutning inom 20 sekunder och försöker ansluta igen av sig självt. Det du ändrar under tiden (volymer, steg, plugin-rattar) skickas när anslutningen är tillbaka. Har någon annan ändrat samma sak under tiden gäller deras ändring, och hint-raden säger hur många av dina som skickades.
- **Värden stänger FLSync eller datorn startas om:** de andra fortsätter i sin egen FL och ansluter igen när värden startar FLSync (sessionen startas om av sig själv, med samma inbjudan och de sparade versionerna kvar).
- **Någon stänger FL:** hen lämnar sessionen. När FL startar igen med projektet ansluter hen och får sessionens aktuella värden.
- **Sparar du medan du är bortkopplad** delas versionen (sammanslagen om det behövs) när anslutningen är tillbaka.

## När datorerna skiljer sig

FLSync är byggt för att fungera även när ni inte har exakt samma uppsättning:

- **Ett plugin saknas hos någon.** FLSync varnar innan versionen öppnas, med vilka kanaler som använder pluginet (`saknas här: Serum (kanal 3, 7)`). Projektet går att öppna ändå. Kanalen låter inte hos den som saknar pluginet, men allt annat synkas som vanligt.
- **Ni har olika FL-versioner** (t.ex. 2025 och 2026). Det fungerar: när du sparar slås ändringarna ihop i din egen FL-version, så din FL öppnar resultatet utan att varna (FL 2024.2, 2025 och 2026 sparar projekt på samma sätt; med äldre FL än så ersätter den senaste sparningen den förra). En version från en nyare FL öppnas aldrig av sig själv (FL varnar för den); Ctrl+S tar in den. Plugins och funktioner som bara finns i den nyare FL kan saknas hos den med den äldre. FLSync-fönstret visar vilken FL-version var och en har.
- **Du installerar en ny FL-version** (t.ex. FL Studio 2026). Starta den en gång och stäng den: har du FLSync igång ställer det in den nya FL-versionen så fort den är stängd (FLSync-fönstret säger till), annars gör Start-menyn > *Reparera FLSync* det. FLSync känner av vilken FL du kör.
- **Egna samples.** Samples från dina egna mappar skickas med och läggs i projektets mapp hos de andra, med projektets sökvägar ändrade dit. FL:s egna samples finns redan hos alla. Samma sample skickas bara en gång.
- **Två ändrar strukturen samtidigt.** Den som först lägger till eller tar bort en kanal eller ett plugin tar "pennan", och de andra ser i hint-raden att `Anna ändrar projektets struktur`. Ändrar någon annan också, varnar FLSync: lägger ni båda till kanaler går versionerna inte att slå ihop, och den som sparar sist ersätter den andras.
- **Någon ändrar strukturen.** Bara den del som ändrades pausas: ett nytt instrument pausar kanalerna, en ny effekt pausar mixern (`FLSync: kanalerna pausade, tryck Ctrl+S för att få Annas version`). Resten, och tempo, play/stop och spellistans spår, synkar som vanligt. Ctrl+S slår ihop de egna ändringarna med den nya versionen och öppnar resultatet. (Skapar FL en ny mixerkanal åt instrumentet pausas mixern också.)
- **FL hänger en stund** (laddar ett plugin eller ett projekt, sparar, visar en dialogruta). FL stannar kvar i sessionen så länge FL-fönstret finns, och det de andra ändrat under tiden kommer fram när FL svarar igen. Först efter två minuter utan svar lämnar FL sessionen, och då ansluter det igen av sig självt när det svarar.
- **Någon har ett annat projekt öppet, eller ansluter sent.** Har hens FL inga osparade ändringar öppnas sessionens version direkt; annars pausas hen tills den är öppnad (*Öppna senaste versionen*).
- **Någon lämnar.** Den som blir ensam kvar fortsätter med sitt projekt som sessionens.

## Noter

FL:s MIDI-script kan inte läsa noter i pianorullen, men pianorulle-script kan, när du startar dem. Installationen lägger till två:

- **FLSync skicka noter** skickar alla noter i pianorullen (mönstret och kanalen du har öppna) till de andra.
- **FLSync hämta noter** ersätter noterna i pianorullen med de senaste någon har skickat för samma mönster och kanal.

Du hittar dem i pianorullens meny *Tools → Scripts*. *Run last script again* (Ctrl+Alt+Y) kör det senaste igen. När någon har skickat noter säger hint-raden t.ex. `FLSync: Anna skickade noter till kanal 5 i mönster 1`. Öppna då pianorullen för den kanalen och kör *FLSync hämta noter*. FLSync måste vara igång.

## FLSync-fönstret

FLSync-fönstret är hela programmet (ett eget fönster utan flikar och adressfält; Windows ritar det med Edge, som finns i alla Windows 10 och 11). Stänger du det avslutas FLSync. Det visar:

- **Status:** är FL igång och hörs det, är sessionen ansluten. Saknas något säger FLSync vad du ska göra, t.ex. att starta loopMIDI, att FL måste startas om efter installationen, att MIDI-inställningarna inte stämmer, att lösenordet är fel eller att värden inte svarar.
- **Session och inbjudan:** starta, gå med, kopiera inbjudan, gör en ny kod, lämna.
- **Deltagare** med deras FL-versioner, och vem som ändrar strukturen just nu.
- **Senaste versionen av projektet:** vem som sparade den och när, samples, plugins som saknas här och om den kräver en nyare FL, och hur din senaste sparning gick (delad, sammanslagen, ersatt). *Öppna senaste versionen* öppnar den i FL.
- **Noter** som andra har skickat, och en logg.

Ditt namn och den senaste inbjudan sparas i `%LOCALAPPDATA%\FLSync\settings.json`, och FLSyncs logg i `flsync.log` där. Fönstret visar en sida som bara nås från din egen dator.

## Krav

- **Windows 10 eller 11.** macOS stöds inte än (MIDI-delen bygger på Windows).
- **FL Studio 2025 eller 2026.** Allt är testat mot 2025.2.4 (script-API 40) och använder bara det som finns i båda; swing synkas i FL 2026. Kanalracket kräver API 33 och plugin-parametrarna API 26.
- **Samma plugins** från andra tillverkare hos alla som ska höra dem (se [När datorerna skiljer sig](#när-datorerna-skiljer-sig)).

## Kända begränsningar

- **Innehåll synkas automatiskt, men inte live.** Nya kanaler och plugins (med sina inställningar), samples, klipp i spellistan, noter och automation sparar FL av sig själv när du har slutat ändra i ett par sekunder (FL 2026.1.7 eller senare; i äldre FL med Ctrl+S). Hos de andra laddas det in när de har stoppat uppspelningen och inte rör något: hint-raden räknar ner (`hämtar Annas ändringar om 3 s`), deras egna ändringar sparas och slås ihop först, och projektet laddas om (ångra-historiken börjar om). Mitt i en uppspelning laddas aldrig något. Live går det inte att göra: FL:s script-API, även i FL 2026, kan inte läsa eller lägga klipp i spellistan, lägga till kanaler eller ladda plugins (den modul Gopher använder för det kan ett MIDI-script inte nå).
- **Sammanslagningen** behåller din ändring där ni båda ändrat samma kanal, mönsterinställning eller mixerkanal (noter och klipp slås ihop ett och ett). Det här går inte att slå ihop: att ni båda lägger till kanaler, att någon tar bort eller flyttar kanaler, mönster eller mixerkanaler, att någon lägger till mixerkanaler, och versioner sparade i olika FL-versioner. Då ersätter den senaste sparningen den förra (som finns kvar i `Versioner`), och båda får veta det.
- **Noter** går bara när någon kör notscripten. FLSync antar att pianorullen visar den kanal som är vald i kanalracket.
- **Två olika projekt med samma filnamn** (t.ex. två `Beat.flp` i olika mappar) kan FLSync blanda ihop när du öppnar det andra under en session. FL visar bara filnamnet, så FLSync ser vilken mapp först när FL sparar. Byt namn på det ena (*File → Save as*) om du byter projekt mitt i en session.
- **Ändringar gjorda medan FLSync-scriptet inte kör i FL** (t.ex. i en FL-version som inte är inställd än): rattar, steg och namn blir sessionens igen när du öppnar en version medan andra är med. Klipp, pianorullens noter, automation och plugins följer med filen som vanligt.
- **Samples inuti plugins** skickas med, men deras sökvägar i pluginets data kan inte skrivas om; FL hittar dem eftersom de ligger i projektets mapp. VST-pluginens egna filer (t.ex. Kontakt-bibliotek) skickas inte.
- **Effektplatsernas på/av** synkas inte: i FL 2025.2.4 kan ett script stänga av en plats men inte slå på den igen. Mixnivån per plats synkas.
- **Taktartens nämnare** går inte att läsa från ett script, så bara täljaren synkas.
- **Plugin-parametrar** skickas bara från det plugin vars fönster du har framme, och de ingår inte i startvärdena. Ett preset-byte i ett stort VST skickar tusentals värden och tar några sekunder att gå fram.
- **Play/stop** går över nätverket, så uppspelningen startar med några millisekunders skillnad.
- Automation som rör synkade värden under uppspelning gör att de "slåss".
- Kanaler av samma sort som byter plats (t.ex. två samplers) märks inte förrän någon sparar, och då bara om de har olika namn.
- Lösenordet skyddar sessionen, men trafiken är inte krypterad. Dela inbjudan bara med dem du spelar med.

## Felsökning

Börja med FLSync-fönstret: det känner igen de flesta problem och säger vad du ska göra. Annars:

- **FLSync saknas i FL:s MIDI-inställningar (Controller type):** starta om FL efter installationen.
- **FL hörs inte:** porten `FLSync` måste finnas innan FL startar. Starta loopMIDI, stäng FL och starta det igen. Välj Start-menyn > *Reparera FLSync* om det inte hjälper.
- **Inget fönster öppnas när jag dubbelklickar på FLSync:** FLSync körs kanske redan, då kommer fönstret fram. Annars står felet i `%LOCALAPPDATA%\FLSync\flsync.log`. Saknar datorn både Edge och Chrome öppnas FLSync i webbläsaren i stället.
- **`FLSync: kanalerna pausade`** (eller mixern): ditt projekt har inte samma kanaler eller plugins som sessionens där. Hint-raden säger om du ska spara eller öppna någon annans version. Resten synkar under tiden.
- **Projektet delas inte när jag sparar:** FLSync ser vilken fil FL skriver när du trycker Ctrl+S (namnet står i FL:s fönsterrubrik). Ett nytt projekt måste sparas som fil först, på en lokal disk (inte en nätverksenhet). Står det att projektet inte hör till sessionen: öppna sessionens version, eller tryck Ctrl+S igen inom 15 sekunder (eller *Dela mitt projekt* i FLSync-fönstret) för att göra ditt projekt till allas.
- **Vad hände?** FLSync-fönstrets logg visar också FL:s egna meddelanden: när FL var upptaget och hur länge, fel i en del av FL och varför synken pausades.
- **Notscripten säger att FLSync inte är igång:** starta FLSync. Scripten läser dess fil i `%LOCALAPPDATA%\FLSync\notes`.
- **`Öppna senaste versionen` öppnade ett nytt FL-fönster:** stäng det nya och öppna filen via *File → Open* i det FL du redan har igång. FLSync visar var filen ligger (*Visa i mappen*).
- **Något i installationen har gått sönder** (t.ex. porten eller FL:s MIDI-inställningar): Start-menyn > *Reparera FLSync*.
- **Avinstallera:** Start-menyn > *Avinstallera FLSync*.

## Så fungerar det

```
FL Studio <--MIDI "FLSync"--> FLSync (brygga) <--TCP--> relay <--> de andras FLSync och FL
 (script)       (loopMIDI)     webbsida, filer           (hos den som startar sessionen)
```

FL låter inte script öppna nätverksanslutningar eller filer. Scriptet i FL pratar därför MIDI med FLSync-programmet bredvid, och programmet pratar med relayn över nätverket och sköter allt som har med filer att göra: projektfiler, samples och noter.

- **Hitta ändringar:** scriptet sparar en kopia av alla värden och jämför med FL. FL:s callbacks (`OnDirtyMixerTrack`, `OnDirtyChannel`, `OnRefresh`) talar om var något har ändrats. Dessutom läses några spår, kanaler, steg och parametrar om i tur och ordning varje gång, utifall FL inte rapporterar en ändring. En genomläsning tar ungefär 0,2 ms av FL:s tid.
- **Satsvis:** ändringar skickas i satser om upp till 64 värden. Medan MIDI-vägen fortfarande skickar tidigare satser väntar nya ändringar, och skickas sedan med sitt senaste värde.
- **Ordning:** relayn ger varje ändring ett löpnummer, sparar det senaste värdet för varje nyckel och skickar ändringarna till alla andra. Avsändaren får en kort kvittens.
- **Konflikter:** tills din egen senaste ändring av ett värde är kvitterad ignorerar scriptet andras ändringar av samma värde. Din ändring ligger senare i relayns ordning och vinner hos alla.
- **Inga ekon:** när scriptet har skrivit in någon annans värde sparar det värdet som FL faktiskt behöll (FL avrundar), så det skickas aldrig tillbaka.
- **Startvärden:** den första FL som ansluter delar sina inställningar. Om någon hinner ändra ett värde medan de är på väg skriver relayn inte över ändringen.
- **Struktur:** scriptet räknar ut ett fingeravtryck i två delar, av projektets kanaler och av mixerns spår och plugins. Ett värde går bara mellan FL där dess del är lika, så att ett värde aldrig hamnar på fel kanal; värden som tempo och play gäller alla. När en sparad version ändrar strukturen börjar relayn om med den versionens värden för den del som ändrades. När du har öppnat en version räknas din struktur som den versionens, även om ett plugin saknas hos dig.
- **Projektversioner:** FLSync bevakar projektfilen. När FL har sparat den läser FLSync vilka samples den använder, laddar upp det relayn inte redan har och meddelar sessionen. Mottagarna hämtar det som saknas till sin cache (`%LOCALAPPDATA%\FLSync\cache`) och skriver projektmappen i `Dokument\Image-Line\FL Studio\Projects\FLSync\<namn>\`.
- **Sammanslagning:** en .flp är en lång rad händelser. FLSync delar upp den i delar (mönstrens noter, kanaler, mönsterinställningar, arrangemang, spellistans klipp, spår, mixerkanaler, mixerparametrar) och jämför varje del i tre versioner: den gemensamma utgångspunkten, din och den senaste. Noter och klipp jämförs ett och ett. Varje delning anger vilken version den bygger på, och relayn tar bara emot den om det fortfarande är den senaste; annars slås den ihop igen med den nyare.
- **Bortkopplad:** bryggan och relayn skickar `ping` till varandra, så en tyst anslutning märks inom 20 sekunder. Scriptet i FL minns vad du ändrar medan det är bortkopplat och skickar det när det är tillbaka, utom det som någon annan har ändrat under tiden.
- **Plugins:** bara det plugin vars fönster du har framme läses, så att arbetet per uppdatering är litet och så att automation i andra plugins inte skickas runt. Andras ändringar skrivs in även om fönstret är stängt, men bara om det sitter samma plugin på samma plats.
- **Stegsekvensern:** FL:s script ser bara det valda mönstret. Ändrar någon steg i ett annat mönster skrivs de in när du byter till det.
- **Play och stop** är händelser, inte tillstånd: den som ansluter mitt i en session startar inte uppspelning.

Mellan FL och programmet går meddelandena som pitch-bend-händelser med löpnummer och kontrollsumma, ungefär 4 000 byte per sekund; därför är nycklarna korta (`m/3/v` är volymen på Insert 3) och filer går aldrig den vägen. Protokollet och alla nycklar finns i [docs/protocol_v2.md](docs/protocol_v2.md), fas 3:s tillägg i [docs/phase3.md](docs/phase3.md) och fas 4:s (sammanslagning, frånkoppling) i [docs/phase4.md](docs/phase4.md).

Varför det ser ut så, med hänsyn till FL Studio 2025.2.4:
- `socket` finns i FL:s Python, men FL stoppar den. MIDI-script får inte heller öppna filer. Pianorulle-script får det, men kör i en egen Python och når inte MIDI-scriptet; därför går noterna via filer.
- SysEx mellan script och program kraschade FL. FL 26.1.x listar rättelser för just det.
- FL levererade ibland varje MIDI-händelse två gånger. Löpnumren sorterar bort dubbletter.

## För utvecklare

| Fil | Vad |
|---|---|
| `FLSync.exe` (`launcher/FLSync.cs`, `scripts/build_exe.py`) | startar `app/flsync.py` med FL:s `pythonw.exe`, utan konsol; från nedladdningen med `--install` |
| `app/flsync.py`, `app/ui.html` | programmet i sitt eget fönster (Edge eller Chrome med `--app`; `--browser` för webbläsaren); `app/` har också inbjudningskoder, UPnP och diagnostik |
| `setup/setup.py`, `app/installer.py` | installationen, i FLSync-fönstret (`--install`, `--repair`, `--uninstall`, `--dry-run`) eller i en konsol: `Installera FLSync.cmd` (`--dry-run`, `--uninstall`, `--yes`) |
| `FLSync.cmd` | programmet med konsol, för utvecklare |
| `fl/FLSync/device_FLSync.py` | MIDI-scriptet som FL Studio laddar |
| `fl/FLSync/flsync_client.py` | synklogiken: ordning, konflikter, startvärden, struktur, pennan, närvaro |
| `fl/FLSync/flsync_mixer.py`, `flsync_channels.py`, `flsync_plugins.py`, `flsync_song.py` | en modul per del av FL |
| `fl/FLSync/flsync_structure.py`, `flsync_notes.py`, `flsync_link.py` | fingeravtrycket, pianorullens kanal, MIDI-formatet |
| `fl/Piano roll scripts/` | notscripten |
| `bridge/bridge.py`, `bridge/flp.py`, `bridge/flp_merge.py` | bryggan (MIDI ↔ relay, projektversioner, noter), FL:s projektfiler och sammanslagningen av dem |
| `relay/relay.py` | sessionsservern |
| `tools/monitor.py` | terminalklient: se ändringar live och skicka egna |
| `scripts/install.ps1` | utvecklarinstallation: länkar scriptet i FL till den här mappen |
| `scripts/make_zip.py` | packar `dist/FLSync.zip` att skicka till de andra (bara det som behövs, inga tester); bygger FLSync.exe först om det behövs |
| `app/version.py`, `CHANGELOG.md` | versionen, och vad varje version innehåller |
| `.github/workflows/` | testerna vid varje push, och releaserna (se nedan) |
| `docs/` | protokoll, API-anteckningar, fas 3 och 4 |

Delarna kan också köras var för sig, med vilken Python 3.10+ som helst:

```
python relay/relay.py --host 0.0.0.0 --password hemligt -v
python bridge/bridge.py --relay 192.168.1.20:31415 --password hemligt --name Anna
python tools/monitor.py --password hemligt
```

| Program | Flaggor |
|---|---|
| `relay.py` | `--host` (standard `127.0.0.1`; `0.0.0.0` för andra datorer), `--port` (31415), `--password`, `--store` (var versioner och samples sparas), `--max-blob` (256M), `-v` |
| `bridge.py` | `--relay`, `--password`, `--name`, `--port` (loopMIDI-porten, `FLSync`), `--project` (annars senaste filen i FL:s *Recent*), `--projects-dir`, `--auto-open` |
| `monitor.py` | `--host`, `--port`, `--name`, `--password`; kommandon som `vol 1 0.5`, `chvol 1 0.5`, `tempo 128`, `play`, `demo 1`, `set <nyckel> <värde>`, `state`, `help` |
| `app/flsync.py` | `--host`, `--join KOD`, `--name`, `--browser`, `--no-browser`, `--no-upnp`, `--relay-port`, `--projects-dir`, `--data-dir`, `--install`, `--repair`, `--uninstall`, `--dry-run`, `--version` |

**Två FL på samma dator** (för att prova): varje FL behöver en egen loopMIDI-port och en egen brygga med egen projektfil och mapp för mottagna versioner, t.ex. `python bridge/bridge.py --port "FLSync 2" --name Test --project C:\FLSync-test\test.flp --projects-dir C:\FLSync-test`. Notscripten fungerar då bara för den ena.

### Tester

```
python -m unittest discover -s tests -v
```

Testerna laddar det riktiga FL-scriptet med låtsasversioner av FL:s moduler, som beter sig som FL 2025.2.4 där det spelar roll: den dynamiska mixern, mute och mixnivåer som visas med fördröjning, tempo som bara går att sätta via ett REC-event, kanalfunktioner med `useGlobalIndex`, stegparametrar som bara finns för steg som är på, en VST med 4 240 parameterplatser och fokus på pluginfönster. De kopplar riktiga bryggor och en riktig relay över TCP och simulerar att FL får varje MIDI-händelse två gånger. FLP-läsaren provas mot alla 161 projektfiler som följer med FL, installationen mot ett låtsasregister och programmet mot en låtsasrouter. De körs med FL 2025:s och FL 2026:s egen Python 3.12 och med Python 3.10 och 3.14.

Provat live i FL Studio 2025.2.4: alla delar läses och skrivs åt båda hållen utan ekon; en annan deltagares noter, sparade version med eget sample och strukturbyte kommer fram och pausar och återupptar synken som de ska.

### Bygga och släppa en ny version

```
python scripts/build_exe.py      # FLSync.exe, med Windows egen C#-kompilator
python scripts/make_zip.py       # dist/FLSync.zip
```

En ny version: ändra `VERSION` i `app/version.py`, skriv vad som är nytt överst i `CHANGELOG.md`, committa, och tagga:

```
git tag v0.9.1
git push origin v0.9.1
```

GitHub Actions kör då testerna på Windows, bygger FLSync.exe och FLSync.zip och lägger upp en release med zip-filen och texten ur `CHANGELOG.md`. Länken till den senaste zip-filen ändras aldrig: `releases/latest/download/FLSync.zip`.

## Licens

MIT, se [LICENSE](LICENSE). FLSync är inte kopplat till eller godkänt av Image-Line. FL Studio är ett varumärke som tillhör Image-Line Software.
