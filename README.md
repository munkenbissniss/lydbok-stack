# Lydbok-stack

En lydbokserver på Docker som setter seg opp selv. Du svarer på noen spørsmål, og etter et par minutter har du et eget «Audible» hjemme: lydbøker og podkaster i nettleseren og på mobilen, og nye bøker som hentes av seg selv når favorittforfatterne dine gir ut noe.

Fungerer på Linux, macOS og Windows. Kjører du [Plex-stack](../../../plex-stack) på samme maskin, kan lydbøkene bruke nedlasterne derfra, slik at du slipper en VPN og en qBittorrent til.

Bygget på ideen i [MTeaHead/bookstack](https://github.com/MTeaHead/bookstack), med automatisk oppsett i stedet for håndarbeid.

## Last ned

| System | Pakke | Start med |
|---|---|---|
| Linux | [lydbok-stack-linux.zip](../../releases/latest/download/lydbok-stack-linux.zip) | `./start.sh` |
| macOS | [lydbok-stack-macos.zip](../../releases/latest/download/lydbok-stack-macos.zip) | dobbeltklikk `Start.command` |
| Windows | [lydbok-stack-windows.zip](../../releases/latest/download/lydbok-stack-windows.zip) | dobbeltklikk `Start.bat` |

Pakkene inneholder det samme. Forskjellen er startskriptene, og Linux-pakken har i tillegg skript for å gjøre en gammel PC eller Mac til hjemmeserver.

## Hva du får

| App | Adresse | Hva den gjør |
|---|---|---|
| Audiobookshelf | `:13378` | Lydbøker og podkaster. Husker hvor du er, på tvers av enheter. Apper for iPhone og Android. |
| Bookshelf | `:8787` | Følger forfattere og serier, og henter lydbøker automatisk. Videreføring av Readarr. |
| Dashboard | `:3100` | Alt på ett sted, med tall fra appene. |
| qBittorrent bak VPN | `:8090` | Torrent-nedlasting med killswitch (gluetun). |
| NZBGet | `:6790` | Usenet-nedlasting (valgfritt). |
| Prowlarr | `:9697` | Indeksere, synkes automatisk til Bookshelf. |
| Kjøling | `:3011` | Viser belastningen og roer ned nedlastingen når maskinen blir varm. |

Nedlasterne (de fire nederste) kjører bare når stacken står alene. Bruker du Plex-stackens, ligger de på Plex-stackens vanlige porter (8080, 6789 og 9696).

Autooppsettet gjør dette for deg:

- lager administratoren i Audiobookshelf, biblioteket «Lydbøker» og, hvis du vil, «Podkaster», og setter språket til norsk
- gir Bookshelf rotmappe, lydbokprofil, ryddige mappenavn (Forfatter/Tittel), hardlinks og nedlastingsklienter
- lager kategorien `audiobooks` i qBittorrent og NZBGet
- kobler Bookshelf til Prowlarr, så indekserne med lydbøker brukes automatisk
- setter samme innlogging på alle appene
- lager dashboardet med nøkler og tall

Det sjekker hva som allerede er gjort, så det er trygt å kjøre igjen så ofte du vil.

## Dette gjør du for hånd

1. **Indeksere i Prowlarr.** Legg til minst én indekser som har lydbøker. Den kobles automatisk til Bookshelf. Har du Plex-stack, holder det å legge den til i Prowlarr der.
2. **Brukere i Audiobookshelf.** Lag en bruker til hver i familien under Innstillinger → Brukere. Da får alle sin egen fremdrift og egne bokmerker.
3. **Appen på mobilen.** Installer «Audiobookshelf» fra App Store eller Google Play, og logg inn med `http://<maskinens-ip>:13378`.

Oppsummeringen på slutten av autooppsettet sier hva som gjenstår hos deg. Den lagres også i `config/OPPSUMMERING.txt`.

## Sammen med Plex-stack

Finner veiviseren Plex-stack på maskinen, spør den om lydbøkene skal bruke nedlasterne og disken derfra. Det anbefales, særlig på en maskin med lite minne. Da:

- startes ingen ekstra VPN, qBittorrent, NZBGet eller Prowlarr
- legges lydbøkene på samme disk som filmene, så nedlastinger hardlinkes rett inn i biblioteket uten å kopieres
- røres bare det som trengs i Plex-stack: kategorien `audiobooks` i qBittorrent og NZBGet, og Bookshelf i Prowlarr
- brukes samme innlogging som i Plex-stack (veiviseren finner den selv)

Plex-stack må kjøre før du starter lydbok-stacken. Bruker Plex-stack en nettverksdeling, bruker lydbøkene den samme.

## Installer Docker

- **Linux:** `start.sh` tilbyr å installere Docker for deg.
- **Mac:** [OrbStack](https://orbstack.dev) (raskest) eller [Docker Desktop](https://www.docker.com/products/docker-desktop/).
- **Windows:** [Docker Desktop](https://www.docker.com/products/docker-desktop/).

## Kjør veiviseren

Startskriptet kjører veiviseren første gang. Den spør om:

- hvor lydbøkene skal ligge (lokal disk, NFS- eller SMB-deling), og tester delingen før den går videre
- om du vil ha podkaster også
- hvor Audiobookshelf skal hente bokinformasjon. Velg Google Books hvis de fleste bøkene er norske
- felles brukernavn og passord (forslag lages for deg)
- VPN og Usenet, hvis stacken står alene
- rolig modus, som anbefales på bærbare og eldre maskiner

Svarene lagres i `.env`. Kjører du veiviseren igjen, er de gamle svarene standardvalg.

Uten startskriptene:

```bash
docker compose run --rm setup
docker compose up -d
docker compose logs -f autoconfig
```

## Lydbøker du har fra før

Legg dem i lydbokmappa, gjerne slik:

```
Lydbøker/
  Jo Nesbø/
    Harry Hole/
      1 - Flaggermusmannen/
        del 01.mp3 …
  Maja Lunde/
    Bienes historie/
      Bienes historie.m4b
```

Audiobookshelf skanner mappa hver natt og finner nye filer med en gang. Én bok per mappe er det viktigste; serie-mappen kan du sløyfe. Finner veiviseren en mappe som heter `Lydbøker` eller `Audiobooks` (øverst eller én mappe ned), brukes den.

## Hente nye lydbøker

Åpne Bookshelf, søk opp en forfatter eller bok og legg den til. Bookshelf søker i indekserne, laster ned med qBittorrent eller NZBGet, og legger boka i `Forfatter/Tittel`. Audiobookshelf ser den med en gang. Nye bøker fra forfattere du følger, hentes automatisk.

## VPN

Torrent-trafikken går gjennom [gluetun](https://github.com/qdm12/gluetun) med killswitch: faller VPN-en ut, stopper nedlastingen. Veiviseren har ProtonVPN, Mullvad, NordVPN, Surfshark, AirVPN og PIA, eller din egen WireGuard-konfig.

## Usenet

Svar ja på Usenet i veiviseren og oppgi leverandøren (f.eks. Newshosting). NZB-indeksere legger du inn i Prowlarr. Får du «Too many connections», senk `USENET_CONNECTIONS` i `.env` og kjør autooppsettet igjen.

## Nettverksdeling

Ligger disken på en annen maskin, velg NFS eller SMB i veiviseren. Docker kobler seg til delingen selv, så ingenting må monteres. Veiviseren tester at den kan lese og skrive. Linux-pakken har også `linux/02-mount-nfs.sh` hvis du heller vil montere delingen på maskinen.

## Rolig modus og kjøling

Rolig modus gir bakgrunnsappene en fast grense for prosessorbruk. Kjøling (`:3011`) følger med på temperatur og belastning, og når maskinen blir varm eller travel, får nedlasterne enda lavere grenser og qBittorrent går over til lave fartsgrenser. Audiobookshelf begrenses aldri, så avspillingen går som normalt. Grensene står i `.env` (`HOT_TEMP`, `HIGH_LOAD` osv.).

## Kjøre oppsettet på nytt

```bash
docker compose run --rm autoconfig
```

Gjør det etter at du har endret `.env`, lagt til indeksere eller hvis noe sto som ikke klart i oppsummeringen.

## Oppdatere

Kjør `oppdater.sh` (Linux), `Oppdater.command` (Mac) eller `Oppdater.bat` (Windows). De henter nye versjoner av appene, starter dem på nytt og kjører autooppsettet.

## Lytte borte

Installer [Tailscale](https://tailscale.com) på serveren og mobilen, så når du Audiobookshelf hjemmefra uten å åpne porter i ruteren. Bruk Tailscale-navnet til serveren i appen.

## Feilsøking

| Problem | Løsning |
|---|---|
| «network plex-stack_default … not found» | Du bruker nedlasterne fra Plex-stack. Start Plex-stack først. |
| Bookshelf finner ingenting | Indekserne mangler lydbokkategorien (3030). Legg til en indekser med lydbøker i Prowlarr. |
| Bookshelf finner ikke en norsk bok | Bokinformasjonen kommer fra Hardcover, og den kan mangle titler. Legg boka rett i lydbokmappa i stedet. |
| Audiobookshelf: innlogging feilet | Passordet er endret i appen. Sett det tilbake, eller rett `ADMIN_PASSWORD` i `.env` og kjør autooppsettet. |
| qBittorrent svarer ikke | VPN-en er ikke oppe. Se `docker logs lydbok-gluetun`. |
| Appen på mobilen finner ikke serveren | Bruk IP-adressen (`http://192.168.x.x:13378`), ikke `localhost`. |

Loggene til en app: `docker logs <navn>` (f.eks. `audiobookshelf`, `bookshelf`).

## Ansvar

Stacken er et verktøy. Du er selv ansvarlig for hva du laster ned, og for å følge opphavsretten der du bor. Bruk den til bøker du har rett til.

## Takk til

[Audiobookshelf](https://www.audiobookshelf.org), [Bookshelf](https://github.com/pennydreadful/bookshelf) og [Readarr](https://github.com/Readarr/Readarr), [Prowlarr](https://prowlarr.com), [qBittorrent](https://www.qbittorrent.org), [NZBGet](https://nzbget.com), [gluetun](https://github.com/qdm12/gluetun), [Homepage](https://gethomepage.dev) og [MTeaHead/bookstack](https://github.com/MTeaHead/bookstack) for ideen.
