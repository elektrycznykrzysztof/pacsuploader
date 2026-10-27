# PACS Uploader

Prosta aplikacja WWW do przesyłania pojedynczych plików i całych katalogów na serwer. Aplikacja zapisuje przesłane dane na wolumenie źródłowym, kopiuje je na eksport NFS do katalogu bieżącego roku, weryfikuje każdy plik sumą SHA-256, a po potwierdzeniu transferu usuwa lokalne pliki paczki.

## Uruchomienie w Dockerze

Wymagane są Docker z wtyczką Compose, klient NFS na hoście Dockera oraz eksport NFS dostępny z tego hosta.

1. Skopiuj `.env.example` do `.env`.
2. Ustaw `APP_TOKEN`, `NFS_SERVER` i `NFS_EXPORT`. Token powinien być długi i losowy.
3. Przygotuj eksport z prawem zapisu dla UID/GID `10001:10001`, pod którym działa aplikacja.
4. Uruchom aplikację:

   ```sh
   docker compose up -d --build
   ```

5. Otwórz `http://localhost:8080` (lub port ustawiony w `WEB_PORT`) i wprowadź `APP_TOKEN`.

Compose montuje eksport NFS jako wolumen Dockera. Jeśli NFS jest chwilowo niedostępny, Docker może nie uruchomić kontenera; po przywróceniu udziału uruchom ponownie usługę. Aplikacja dodatkowo sprawdza, czy katalog docelowy rzeczywiście jest zamontowany jako NFS.

## Działanie kolejki

- Przeglądarka wysyła pliki wraz z nazwami i ścieżkami względnymi, więc zachowuje się struktura katalogów. Nazwy plików źródłowych nie dostarczają metadanych pacjenta. Zwykłe pliki zachowują nazwy; PNG/JPEG i PDF są konwertowane do `.dcm`. Na NFS trafiają pod `NFS_EXPORT/<rok>/<ścieżka>`.
- Po dodaniu paczki zawierającej PNG/JPEG lub PDF formularz prosi o imię, nazwisko, PESEL, rodzaj badania, stronę (OP/OL/OU), nazwę urządzenia i datę badania. Te dane dotyczą wszystkich konwertowanych plików z danej paczki. Data domyślnie pochodzi z czasu ostatniej modyfikacji pliku udostępnianego przez przeglądarkę i można ją zmienić. Przeglądarki nie udostępniają aplikacji WWW daty utworzenia pliku.
- Obrazy PNG/JPEG są konwertowane do DICOM Secondary Capture. PDF jest przesyłany jako DICOM Encapsulated PDF z oryginalnymi bajtami dokumentu osadzonymi w pliku `.dcm`; limit rozmiaru PDF wynosi 4 GiB (ograniczenie długości elementu DICOM). Patient Name jest zapisywane jako `Nazwisko^Imię`, PESEL jako Patient ID, nazwa urządzenia jako Modality, a rodzaj badania wraz ze stroną jako Study Description `(0008,1030)`.
- Nazwa wynikowego DICOM zawiera imię, nazwisko, PESEL, datę badania, nazwę urządzenia i timestamp uploadu w formacie `YYYYMMDDTHHMMSSffffff`, np. `Imię_Nazwisko_<PESEL>_20261021_CT_20261021T102030123456.dcm`. Timestamp zapewnia unikalność nazw plików.
- Study Date/Time, Content Date/Time oraz Acquisition DateTime w obiektach DICOM bazują na dacie badania z formularza i czasie odczytanym z daty modyfikacji pierwszego konwertowanego pliku w paczce.
- Paczki są zapisywane w wolumenie `fileloader-data` i widoczne na liście transferów.
- Worker automatycznie tworzy katalog roku, kopiuje pliki do ukrytego katalogu tymczasowego w jego obrębie, sprawdza rozmiary i SHA-256, a potem publikuje je pod przygotowanymi ścieżkami. Nie zmienia nazw zwykłych plików ani struktury katalogów.
- Aplikacja nadaje katalogom uprawnienia odczytu i przechodzenia dla wszystkich (`r-x`), a plikom odczyt dla wszystkich (`r--`), zachowując istniejące uprawnienia zapisu. Jeśli serwer NFS odmówi zmiany praw istniejącego katalogu lub pliku, aplikacja zapisuje ostrzeżenie i kontynuuje transfer.
- Rok jest ustalany przy pierwszej próbie transferu według zegara systemowego kontenera i pozostaje przypisany do paczki przy ponowieniach. Nowe wysyłki po zmianie roku trafiają do nowego katalogu, np. `2027`.
- Istniejące pliki o tej samej ścieżce nie są nadpisywane. Transfer czeka na rozwiązanie konfliktu, a lokalna kopia pozostaje na serwerze źródłowym.
- Po restarcie worker porównuje już opublikowane pliki sumami SHA-256 i kończy transfer bez duplikowania danych. Źródłowe pliki usuwa dopiero po potwierdzeniu wszystkich plików docelowych.
- Przy innym błędzie źródłowe pliki pozostają na wolumenie, a worker ponawia próbę z rosnącym odstępem (maksymalnie 15 minut). Można też wybrać „Wyślij ponownie teraz”.
- Historia statusów pozostaje w `fileloader-data`; dane plików lokalnych są usuwane po sukcesie.
- Lista w interfejsie pokazuje maksymalnie 100 najnowszych transferów. Licznik obok kolejki pokazuje wyłącznie paczki aktualnie wysyłane; gdy żadna nie jest wysyłana, pokazuje `0`.
- Tymczasowe pliki odbierane przez serwer są zapisywane w `fileloader-data/.upload-tmp`, a nie w ograniczonym do 256 MiB `/tmp`. Zapewnij na wolumenie miejsce na trwający upload, jego kopię roboczą i już oczekujące paczki.

Upuszczenie katalogu odczytuje jego zawartość rekurencyjnie i zachowuje nazwę katalogu oraz ścieżki plików. Wybór katalogu z przycisku wymaga przeglądarki obsługującej `webkitdirectory` (Chrome, Edge lub Chromium). Puste katalogi nie są przesyłane przez przeglądarki w tym trybie. Domyślny limit pojedynczego żądania wynosi 50 GiB i można go zmienić przez `MAX_UPLOAD_BYTES`.

## Konfiguracja

| Zmienna | Znaczenie | Domyślna wartość |
|---|---|---|
| `APP_TOKEN` | Wspólny token dostępu do kolejki i uploadu | wymagany |
| `NFS_SERVER` | Adres IP lub nazwa hosta serwera NFS | wymagany |
| `NFS_EXPORT` | Eksport NFS, np. `/srv/nfs/incoming` | wymagany |
| `NFS_VERSION` | Wersja protokołu NFS | `4.1` |
| `WEB_PORT` | Port HTTP strony na hoście | `8080` |
| `MAX_UPLOAD_BYTES` | Maksymalny rozmiar paczki w bajtach | `53687091200` |
| `REQUIRE_NFS` | Odrzuć wysyłkę, jeśli docelowy katalog nie jest NFS | `true` |

## Dane i kopie zapasowe

Wolumen `fileloader-data` przechowuje oczekujące paczki i historię transferów. Nie należy usuwać ani czyścić tego wolumenu, jeśli są w nim paczki oczekujące lub ponawiane. Dane docelowe znajdują się na eksporcie NFS.
