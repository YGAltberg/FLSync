# Säkerhet

**Hittar du ett säkerhetsproblem:** rapportera det privat via GitHubs *Security > Report a vulnerability* i det här repot, inte i ett öppet ärende.

Bra att veta om hur FLSync fungerar:

- **Den som startar en session** kör en server (relayn) som lyssnar på TCP-port 31415 på alla nätverkskort. Med UPnP ber FLSync routern att öppna porten mot internet så länge sessionen pågår, och stänger den igen efteråt.
- **Sessionen skyddas av ett slumpat lösenord** som finns i inbjudningskoden. Den som har koden kommer in: dela den bara med dem du spelar med, och gör en ny kod (*Gör en ny kod*) om den har spridits.
- **Trafiken är inte krypterad.** Ändringar, projektfiler och samples går okrypterat mellan datorerna. Vill ni skydda den över internet: använd ett VPN som Tailscale, som FLSync hittar själv.
- **FLSync-fönstret** visar en sida som bara nås från den egna datorn (127.0.0.1) och kräver en nyckel som skapas vid varje start.
- **FLSync tar emot projektfiler och samples** från de andra i sessionen och skriver dem till `Dokument\Image-Line\FL Studio\Projects\FLSync`. Spela bara med personer du litar på.
