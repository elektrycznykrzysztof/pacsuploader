import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import threading
import time
import uuid
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from flask import Flask, jsonify, render_template, request
from PIL import Image, ImageOps, UnidentifiedImageError
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import EncapsulatedPDFStorage, ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
import psycopg
from psycopg.rows import dict_row
from werkzeug.exceptions import RequestEntityTooLarge


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("fileloader")

IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
DOCUMENT_FORMATS = {".pdf": "PDF"}
IMPLEMENTATION_CLASS_UID = generate_uid()


def database_connection():
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "postgres"),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DB_NAME", "fileloader"),
        user=os.environ.get("DB_USER", "fileloader"),
        password=os.environ.get("DB_PASSWORD", ""),
        connect_timeout=5,
        row_factory=dict_row,
    )


def initialize_patient_database():
    attempts = int(os.environ.get("DB_CONNECT_ATTEMPTS", "30"))
    for attempt in range(1, attempts + 1):
        try:
            with database_connection() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS patients (
                        patient_id VARCHAR(11) PRIMARY KEY
                            CHECK (patient_id ~ '^[0-9]{11}$'),
                        first_name VARCHAR(64) NOT NULL,
                        last_name VARCHAR(64) NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            logger.info("Baza pacjentów PostgreSQL jest gotowa.")
            return
        except psycopg.Error:
            if attempt == attempts:
                raise
            logger.warning("PostgreSQL niedostępny; ponawiam połączenie (%s/%s).", attempt, attempts)
            time.sleep(min(2, attempt))


def get_or_create_patient(patient_id, first_name, last_name):
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO patients (patient_id, first_name, last_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (patient_id) DO NOTHING
            """,
            (patient_id, first_name, last_name),
        )
        return connection.execute(
            "SELECT patient_id, first_name, last_name FROM patients WHERE patient_id = %s",
            (patient_id,),
        ).fetchone()


class UploadValidationError(Exception):
    """An uploaded file is valid HTTP input but cannot be accepted by this app."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path, value):
    path = Path(path)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, separators=(",", ":"))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


def read_json(path):
    with Path(path).open("r", encoding="utf-8") as file:
        return json.load(file)


def normalized_relative_path(raw_path):
    if not raw_path or "\x00" in raw_path or "\\" in raw_path:
        raise ValueError("Nieprawidłowa ścieżka pliku.")
    if raw_path.startswith("/") or PureWindowsPath(raw_path).drive:
        raise ValueError("Ścieżka musi być względna.")
    pieces = raw_path.split("/")
    if any(part in ("", ".", "..") for part in pieces):
        raise ValueError("Ścieżka zawiera niedozwolony segment.")
    if len(raw_path.encode("utf-8")) > 4096 or any(len(part.encode("utf-8")) > 255 for part in pieces):
        raise ValueError("Ścieżka pliku jest zbyt długa.")
    if any(any(ord(char) < 32 for char in part) for part in pieces):
        raise ValueError("Ścieżka zawiera niedozwolone znaki.")
    return "/".join(pieces)


def valid_pesel(value):
    if not re.fullmatch(r"[0-9]{11}", value):
        return False
    weights = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
    checksum = (10 - sum(int(digit) * weight for digit, weight in zip(value[:10], weights)) % 10) % 10
    if checksum != int(value[10]):
        return False

    year = int(value[:2])
    encoded_month = int(value[2:4])
    day = int(value[4:6])
    for offset, century in ((80, 1800), (0, 1900), (20, 2000), (40, 2100), (60, 2200)):
        month = encoded_month - offset
        if 1 <= month <= 12:
            try:
                date(century + year, month, day)
                return True
            except ValueError:
                return False
    return False


def conversion_metadata_for_file(relative_path, patient_metadata):
    path = PurePosixPath(relative_path)
    extension = path.suffix.lower()
    expected_format = IMAGE_FORMATS.get(extension)
    document_format = DOCUMENT_FORMATS.get(extension)
    if not expected_format and not document_format:
        return None

    return {
        **patient_metadata,
        "expected_format": expected_format,
        "document_format": document_format,
        "kind": "pdf" if document_format else "image",
    }


def patient_metadata_from_form(form):
    given_name = form.get("first_name", "").strip()
    family_name = form.get("last_name", "").strip()
    pesel = form.get("patient_id", "").strip()
    exam_type = form.get("exam_type", "").strip()
    laterality = form.get("laterality", "").strip()
    device_name = form.get("device_name", "").strip()
    study_date_value = form.get("study_date", "").strip()
    study_time_value = form.get("study_time", "").strip()

    if not given_name or not family_name or any(char in "^=" or ord(char) < 32 for char in given_name + family_name):
        raise UploadValidationError("Wpisz prawidłowe imię i nazwisko.")
    if len(given_name) + len(family_name) + 1 > 64:
        raise UploadValidationError("Imię lub nazwisko jest zbyt długie.")
    if not valid_pesel(pesel):
        raise UploadValidationError("PESEL musi mieć 11 cyfr i prawidłową sumę kontrolną oraz datę.")
    if not exam_type or any(ord(char) < 32 for char in exam_type):
        raise UploadValidationError("Wpisz rodzaj badania.")
    if laterality not in {"OP", "OL", "OU"}:
        raise UploadValidationError("Wybierz stronę badania: OP, OL lub OU.")
    modality = device_name.upper()
    if not re.fullmatch(r"[A-Z0-9_ ]{1,16}", modality):
        raise UploadValidationError("Nazwa urządzenia musi mieć maksymalnie 16 znaków: litery A-Z, cyfry, spacje lub podkreślenia.")

    try:
        study_date = date.fromisoformat(study_date_value)
        study_time = datetime.strptime(study_time_value, "%H:%M:%S").time()
    except ValueError:
        raise UploadValidationError("Data badania lub czas pliku są nieprawidłowe.") from None

    study_description = f"{exam_type} {laterality}"
    if len(study_description) > 64:
        raise UploadValidationError("Rodzaj badania jest zbyt długi dla pola Study Description (maksymalnie 64 znaki).")

    return {
        "given_name": given_name,
        "family_name": family_name,
        "patient_id": pesel,
        "patient_name": f"{family_name}^{given_name}",
        "modality": modality,
        "device_name": device_name,
        "study_date": study_date,
        "study_datetime": datetime.combine(study_date, study_time),
        "study_description": study_description,
    }


def safe_filename_component(value):
    return re.sub(r"[^\w-]+", "_", value, flags=re.UNICODE).strip("._-") or "plik"


def dicom_output_path(relative_path, metadata, timestamp):
    path = PurePosixPath(relative_path)
    study_date = metadata["study_date"].strftime("%Y%m%d")
    filename = "_".join((
        safe_filename_component(metadata["given_name"]),
        safe_filename_component(metadata["family_name"]),
        metadata["patient_id"],
        study_date,
        safe_filename_component(metadata["device_name"]),
        timestamp,
    )) + ".dcm"
    return str(path.with_name(filename))


def convert_pdf_to_dicom(source_path, destination_path, metadata, study_uid, series_uid, series_number, instance_number, study_datetime):
    source_path = Path(source_path)
    destination_path = Path(destination_path)
    try:
        source_size = source_path.stat().st_size
        with source_path.open("rb") as source_file:
            if source_file.read(5) != b"%PDF-":
                raise UploadValidationError("Plik z rozszerzeniem PDF nie ma prawidłowego nagłówka PDF.")
        if source_size == 0:
            raise UploadValidationError("Plik PDF jest pusty.")
        # Explicit VR OB values have a 32-bit length field; leave an even byte
        # available for DICOM padding when the source document has odd length.
        if source_size > 0xFFFFFFFE:
            raise UploadValidationError("Plik PDF jest zbyt duży dla formatu DICOM (maksymalnie 4 GiB).")
    except UploadValidationError:
        raise
    except OSError:
        raise UploadValidationError("Nie udało się odczytać pliku PDF.") from None

    now = study_datetime
    sop_instance_uid = generate_uid()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = EncapsulatedPDFStorage
    file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = IMPLEMENTATION_CLASS_UID
    file_meta.ImplementationVersionName = "PACS_UPLOADER_1"

    dataset = FileDataset(str(destination_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SpecificCharacterSet = "ISO_IR 192"
    dataset.SOPClassUID = EncapsulatedPDFStorage
    dataset.SOPInstanceUID = sop_instance_uid
    dataset.PatientName = metadata["patient_name"]
    dataset.PatientID = metadata["patient_id"]
    dataset.PatientBirthDate = ""
    dataset.PatientSex = ""
    dataset.StudyInstanceUID = study_uid
    dataset.StudyDate = now.strftime("%Y%m%d")
    dataset.StudyTime = now.strftime("%H%M%S.%f")
    dataset.AccessionNumber = ""
    dataset.ReferringPhysicianName = ""
    dataset.StudyID = ""
    dataset.StudyDescription = metadata["study_description"]
    dataset.Modality = metadata["modality"]
    dataset.SeriesInstanceUID = series_uid
    dataset.SeriesNumber = str(series_number)
    dataset.InstanceNumber = str(instance_number)
    dataset.ContentDate = now.strftime("%Y%m%d")
    dataset.ContentTime = now.strftime("%H%M%S.%f")
    dataset.AcquisitionDateTime = now.strftime("%Y%m%d%H%M%S.%f")
    dataset.BurnedInAnnotation = "YES"
    dataset.DocumentTitle = metadata["study_description"]
    dataset.Manufacturer = "PACS Uploader"
    dataset.ManufacturerModelName = "PACS Uploader"
    dataset.SoftwareVersions = "1"
    dataset.ConversionType = "DI"
    dataset.SecondaryCaptureDeviceManufacturer = "PACS Uploader"

    try:
        # Write the DICOM metadata first, then stream the PDF into its OB value.
        # This avoids loading large source documents into memory.
        dataset.save_as(str(destination_path), enforce_file_format=True)
        value_length = source_size + (source_size % 2)
        with destination_path.open("ab") as dicom_file, source_path.open("rb") as source_file:
            dicom_file.write(struct.pack("<HH2s2sI", 0x0042, 0x0011, b"OB", b"\0\0", value_length))
            copied = 0
            while True:
                chunk = source_file.read(1024 * 1024)
                if not chunk:
                    break
                dicom_file.write(chunk)
                copied += len(chunk)
            if copied != source_size:
                raise OSError("Rozmiar pliku PDF zmienił się podczas konwersji.")
            if source_size % 2:
                dicom_file.write(b"\0")
            mime_value = b"application/pdf"
            if len(mime_value) % 2:
                mime_value += b" "
            dicom_file.write(struct.pack("<HH2sH", 0x0042, 0x0012, b"LO", len(mime_value)))
            dicom_file.write(mime_value)
            # UL is a short explicit-VR type: its value length is 16-bit.
            dicom_file.write(struct.pack("<HH2sH", 0x0042, 0x0015, b"UL", 4))
            dicom_file.write(struct.pack("<I", source_size))
            dicom_file.flush()
            os.fsync(dicom_file.fileno())
    except UploadValidationError:
        raise
    except (OSError, ValueError, TypeError, struct.error):
        raise UploadValidationError("Nie udało się utworzyć pliku DICOM z dokumentu PDF.") from None


def convert_image_to_dicom(source_path, destination_path, metadata, study_uid, series_uid, series_number, instance_number, study_datetime):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source_path) as source:
                if source.format != metadata["expected_format"]:
                    raise UploadValidationError("Rozszerzenie pliku nie odpowiada jego formatowi PNG/JPEG.")
                if getattr(source, "n_frames", 1) != 1:
                    raise UploadValidationError("Animowane i wieloklatkowe obrazy PNG/JPEG nie są obsługiwane.")
                source.load()
                image = ImageOps.exif_transpose(source)
                has_alpha = "A" in image.getbands() or "transparency" in image.info
                if has_alpha:
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, (255, 255, 255))
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                elif image.mode in ("I;16", "I;16B", "I;16L"):
                    image = image.convert("I;16")
                elif image.mode == "I":
                    minimum, maximum = image.getextrema()
                    if minimum < 0 or maximum > 65535:
                        raise UploadValidationError("Obraz zawiera nieobsługiwany zakres wartości pikseli.")
                    image = image.convert("I;16")
                elif image.mode in ("1", "L"):
                    image = image.convert("L")
                elif image.mode == "F":
                    raise UploadValidationError("Obraz zawiera nieobsługiwany format pikseli zmiennoprzecinkowych.")
                else:
                    image = image.convert("RGB")
    except UploadValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise UploadValidationError("Nie udało się odczytać obrazu PNG/JPEG lub jego rozmiar jest zbyt duży.") from None

    now = study_datetime
    sop_instance_uid = generate_uid()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = IMPLEMENTATION_CLASS_UID
    file_meta.ImplementationVersionName = "PACS_UPLOADER_1"

    dataset = FileDataset(str(destination_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SpecificCharacterSet = "ISO_IR 192"
    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = sop_instance_uid
    dataset.PatientName = metadata["patient_name"]
    dataset.PatientID = metadata["patient_id"]
    dataset.PatientBirthDate = ""
    dataset.PatientSex = ""
    dataset.StudyInstanceUID = study_uid
    dataset.StudyDate = now.strftime("%Y%m%d")
    dataset.StudyTime = now.strftime("%H%M%S.%f")
    dataset.AccessionNumber = ""
    dataset.ReferringPhysicianName = ""
    dataset.StudyID = ""
    dataset.StudyDescription = metadata["study_description"]
    dataset.Modality = metadata["modality"]
    dataset.SeriesInstanceUID = series_uid
    dataset.SeriesNumber = str(series_number)
    dataset.AcquisitionNumber = "1"
    dataset.InstanceNumber = str(instance_number)
    dataset.ImageType = ["DERIVED", "SECONDARY"]
    dataset.ContentDate = now.strftime("%Y%m%d")
    dataset.ContentTime = now.strftime("%H%M%S.%f")
    dataset.AcquisitionDateTime = now.strftime("%Y%m%d%H%M%S.%f")
    dataset.ConversionType = "DI"
    dataset.SecondaryCaptureDeviceManufacturer = "PACS Uploader"

    if image.mode in ("L", "I;16"):
        dataset.SamplesPerPixel = 1
        dataset.PhotometricInterpretation = "MONOCHROME2"
        pixel_data = image.tobytes()
        if image.mode == "I;16" and sys.byteorder == "big":
            pixel_data = b"".join(pixel_data[index:index + 2][::-1] for index in range(0, len(pixel_data), 2))
    else:
        dataset.SamplesPerPixel = 3
        dataset.PhotometricInterpretation = "RGB"
        dataset.PlanarConfiguration = 0
        pixel_data = image.tobytes()

    dataset.Rows, dataset.Columns = image.height, image.width
    bits_allocated = 16 if image.mode == "I;16" else 8
    dataset.BitsAllocated = bits_allocated
    dataset.BitsStored = bits_allocated
    dataset.HighBit = bits_allocated - 1
    dataset.PixelRepresentation = 0
    if len(pixel_data) % 2:
        pixel_data += b"\0"
    dataset.PixelData = pixel_data
    if metadata["expected_format"] == "JPEG":
        dataset.LossyImageCompression = "01"
        dataset.LossyImageCompressionMethod = "ISO_10918_1"
    try:
        dataset.save_as(str(destination_path), enforce_file_format=True)
    except (OSError, ValueError, TypeError):
        raise UploadValidationError("Nie udało się utworzyć pliku DICOM dla obrazu.") from None


def mount_type_for(path, mountinfo_path="/proc/self/mountinfo"):
    """Return the filesystem type for the deepest mount containing path."""
    path = os.path.realpath(path)
    best_mount = None
    best_type = None
    try:
        with open(mountinfo_path, "r", encoding="utf-8") as mounts:
            for line in mounts:
                left, separator, right = line.rstrip("\n").partition(" - ")
                if not separator:
                    continue
                fields = left.split()
                post_fields = right.split()
                if len(fields) < 5 or not post_fields:
                    continue
                mount_point = fields[4]
                for escaped, decoded in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
                    mount_point = mount_point.replace(escaped, decoded)
                mount_point = os.path.realpath(mount_point)
                try:
                    contained = os.path.commonpath([path, mount_point]) == mount_point
                except ValueError:
                    contained = False
                if contained and (best_mount is None or len(mount_point) > len(best_mount)):
                    best_mount = mount_point
                    best_type = post_fields[0]
    except OSError:
        return None
    return best_type


class NfsMonitor(threading.Thread):
    def __init__(self, nfs_root, interval=10, stale_after=30):
        super().__init__(name="nfs-health-monitor", daemon=True)
        self.nfs_root = Path(nfs_root)
        self.interval = interval
        self.stale_after = stale_after
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._connected = None
        self._checked_at = None
        self._completed_at = None

    def _check(self):
        if not self.nfs_root.is_dir():
            raise OSError(f"Katalog NFS nie jest dostępny: {self.nfs_root}")
        filesystem = mount_type_for(self.nfs_root)
        if not filesystem or not filesystem.startswith("nfs"):
            raise OSError(f"{self.nfs_root} nie jest zamontowany jako NFS.")

        # statvfs forces an NFS request. A short-lived write/read/remove probe
        # also confirms that this container can use the export for transfers.
        os.statvfs(self.nfs_root)
        year_root = self.nfs_root / str(datetime.now().year)
        try:
            year_root.mkdir(mode=0o755)
        except FileExistsError:
            pass
        mode = year_root.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise OSError(f"Nieprawidłowy katalog roczny na NFS: {year_root}")
        probe = year_root / f".fileloader-health-{uuid.uuid4().hex}"
        descriptor = None
        try:
            descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.write(descriptor, b"ok")
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.read(descriptor, 2) != b"ok":
                raise OSError("Testowy odczyt z NFS zwrócił nieprawidłowe dane.")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            probe.unlink(missing_ok=True)

    def snapshot(self):
        with self._lock:
            connected = self._connected
            checked_at = self._checked_at
            completed_at = self._completed_at
        if completed_at is None:
            return {"state": "checking", "connected": None, "checked_at": None}
        if time.monotonic() - completed_at > self.stale_after:
            return {"state": "disconnected", "connected": False, "checked_at": checked_at}
        return {
            "state": "connected" if connected else "disconnected",
            "connected": bool(connected),
            "checked_at": checked_at,
        }

    def run(self):
        while not self.stop_event.is_set():
            error = None
            try:
                self._check()
                connected = True
            except Exception as caught_error:
                connected = False
                error = caught_error

            with self._lock:
                previous = self._connected
                self._connected = connected
                self._checked_at = utc_now()
                self._completed_at = time.monotonic()

            if connected and previous is not True:
                logger.info("Połączenie z NFS działa prawidłowo.")
            elif not connected and previous is not False:
                logger.warning("NFS jest niedostępny: %s", error)
            self.stop_event.wait(self.interval)


class TransferWorker(threading.Thread):
    def __init__(self, data_root, nfs_root, require_nfs):
        super().__init__(name="nfs-transfer-worker", daemon=True)
        self.data_root = Path(data_root)
        self.jobs_root = self.data_root / "jobs"
        self.nfs_root = Path(nfs_root)
        self.require_nfs = require_nfs
        self.stop_event = threading.Event()

    def run(self):
        self._recover_interrupted_jobs()
        logger.info("Worker transferu uruchomiony; katalog docelowy: %s", self.nfs_root)
        while not self.stop_event.is_set():
            job_dir, job = self._next_job()
            if not job:
                self.stop_event.wait(2)
                continue
            try:
                job["state"] = "transferring"
                job["attempts"] = int(job.get("attempts", 0)) + 1
                # Pin the year on the first transfer attempt so retries resume in
                # the same annual directory, even if the calendar year changes.
                if not job.get("destination_year"):
                    job["destination_year"] = str(datetime.now().year)
                job["updated_at"] = utc_now()
                self._save_job(job_dir, job)
                destination = self._deliver(job_dir, job)
                job["state"] = "delivered"
                job["destination"] = "magazynie NFS" if destination == self.nfs_root else destination.name
                job["error"] = None
                job["next_attempt_at"] = None
                job["updated_at"] = utc_now()
                payload_dir = job_dir / "payload"
                for retained_dir in (payload_dir, job_dir / "sources"):
                    if retained_dir.exists():
                        shutil.rmtree(retained_dir)
                self._save_job(job_dir, job)
                logger.info("Paczka %s została wysłana do %s", job["id"], destination)
            except Exception as error:  # Keep source files and retry after a delay.
                logger.exception("Wysyłka paczki %s nie powiodła się", job.get("id", "?"))
                job["state"] = "waiting"
                job["error"] = str(error)[:500]
                delay = min(30 * (2 ** max(0, int(job.get("attempts", 1)) - 1)), 900)
                job["next_attempt_at"] = time.time() + delay
                job["updated_at"] = utc_now()
                try:
                    self._save_job(job_dir, job)
                except OSError:
                    logger.exception("Nie udało się zapisać stanu paczki %s", job.get("id", "?"))

    def _recover_interrupted_jobs(self):
        for job_dir in self.jobs_root.iterdir():
            job_path = job_dir / "job.json"
            if not job_path.is_file():
                continue
            try:
                job = read_json(job_path)
                if job.get("state") == "transferring":
                    job["state"] = "queued"
                    job["next_attempt_at"] = None
                    job["updated_at"] = utc_now()
                    self._save_job(job_dir, job)
            except (OSError, ValueError, json.JSONDecodeError):
                logger.exception("Nie udało się odtworzyć stanu %s", job_dir)

    def _next_job(self):
        now = time.time()
        try:
            job_dirs = sorted(self.jobs_root.iterdir(), key=lambda path: path.name)
        except OSError:
            logger.exception("Nie udało się odczytać kolejki paczek")
            return None, None
        for job_dir in job_dirs:
            job_path = job_dir / "job.json"
            if not job_path.is_file():
                continue
            try:
                job = read_json(job_path)
            except (OSError, ValueError, json.JSONDecodeError):
                logger.exception("Nieprawidłowy plik stanu %s", job_path)
                continue
            if job.get("state") == "queued":
                return job_dir, job
            if job.get("state") == "waiting" and float(job.get("next_attempt_at") or 0) <= now:
                return job_dir, job
        return None, None

    def _save_job(self, job_dir, job):
        atomic_write_json(Path(job_dir) / "job.json", job)

    def _destination(self, job):
        # Legacy destination format from older releases.
        return self.nfs_root / f"{job['name']}-{job['id']}"

    def _is_completed(self, destination, job):
        marker = destination / ".fileloader-transfer.json"
        if not destination.is_dir() or not marker.is_file():
            return False
        try:
            saved = read_json(marker)
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        return saved.get("job_id") == job["id"] and saved.get("files") == job.get("files")

    def _target_matches(self, target, item):
        if not os.path.lexists(target):
            return False
        relative = item["path"]
        if target.is_symlink() or not target.is_file():
            raise RuntimeError(f"Ścieżka docelowa już istnieje i nie jest zwykłym plikiem: {relative}")
        if target.stat().st_size != int(item["size"]):
            raise RuntimeError(f"Plik o tej nazwie już istnieje na NFS: {relative}")
        digest = hashlib.sha256()
        with target.open("rb") as target_file:
            while True:
                chunk = target_file.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        if digest.hexdigest() != item["sha256"]:
            raise RuntimeError(f"Plik o tej nazwie już istnieje na NFS: {relative}")
        return True

    def _ensure_directory(self, path):
        created = False
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            try:
                path.mkdir(mode=0o755)
                created = True
            except FileExistsError:
                pass
            mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise RuntimeError(f"Nie można utworzyć katalogu docelowego: {path}")
        # Add read and traversal permission for everyone while preserving
        # existing owner/group write permissions on shared NFS directories.
        try:
            path.chmod(stat.S_IMODE(mode) | 0o055)
        except PermissionError:
            if created:
                raise
            logger.warning("Brak uprawnień do nadania publicznego odczytu katalogowi NFS %s", path)

    def _ensure_file_readable(self, path):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise RuntimeError(f"Nie można ustawić uprawnień pliku docelowego: {path}")
        # Add read permission for everyone, preserving existing write/execute bits.
        try:
            path.chmod(stat.S_IMODE(mode) | 0o044)
        except PermissionError:
            logger.warning("Brak uprawnień do nadania publicznego odczytu plikowi NFS %s", path)

    def _ensure_target_parent(self, root, parts):
        current = root
        for part in parts:
            current = current / part
            self._ensure_directory(current)

    def _deliver(self, job_dir, job):
        if not self.nfs_root.is_dir():
            raise RuntimeError(f"Katalog NFS nie jest dostępny: {self.nfs_root}")
        if self.require_nfs:
            filesystem = mount_type_for(self.nfs_root)
            if not filesystem or not filesystem.startswith("nfs"):
                raise RuntimeError(f"{self.nfs_root} nie jest zamontowany jako NFS (wykryto: {filesystem or 'brak'}).")

        year = str(job.get("destination_year") or datetime.now().year)
        if not re.fullmatch(r"[0-9]{4}", year):
            raise RuntimeError("Nieprawidłowy rok katalogu docelowego.")
        year_root = self.nfs_root / year
        self._ensure_directory(year_root)

        # Finish cleanup for a transfer completed by an older application version.
        legacy_destination = self._destination(job)
        if self._is_completed(legacy_destination, job):
            for retained_dir in (job_dir / "payload", job_dir / "sources"):
                if retained_dir.exists():
                    shutil.rmtree(retained_dir)
            return legacy_destination

        staging = year_root / f".fileloader-{job['id']}.partial"
        if staging.exists():
            if staging.is_symlink():
                raise RuntimeError("Katalog tymczasowy transferu jest dowiązaniem symbolicznym.")
            shutil.rmtree(staging)
        staging.mkdir(mode=0o750)
        payload_dir = job_dir / "payload"
        for item in job.get("files", []):
            relative = normalized_relative_path(item["path"])
            parts = PurePosixPath(relative).parts
            final_path = year_root.joinpath(*parts)
            self._ensure_target_parent(year_root, parts[:-1])
            if self._target_matches(final_path, item):
                self._ensure_file_readable(final_path)
                continue

            source = payload_dir.joinpath(*parts)
            staged_path = staging.joinpath(*parts)
            if not payload_dir.is_dir() or source.is_symlink() or not source.is_file():
                raise RuntimeError(f"Brakuje lokalnego pliku: {relative}")
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            copied = 0
            with source.open("rb") as source_file, staged_path.open("wb") as target_file:
                while True:
                    chunk = source_file.read(1024 * 1024)
                    if not chunk:
                        break
                    target_file.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                target_file.flush()
                os.fsync(target_file.fileno())
            if copied != int(item["size"]) or digest.hexdigest() != item["sha256"]:
                raise RuntimeError(f"Weryfikacja pliku nie powiodła się: {relative}")

            staged_mode = staged_path.stat().st_mode
            staged_path.chmod(stat.S_IMODE(staged_mode) | 0o044)

            if not self._target_matches(final_path, item):
                try:
                    # A hard link publishes the fully verified file atomically and
                    # fails safely if another writer created the name meanwhile.
                    os.link(staged_path, final_path)
                except FileExistsError:
                    self._target_matches(final_path, item)
                staged_path.unlink(missing_ok=True)
            if not self._target_matches(final_path, item):
                raise RuntimeError(f"Nie udało się potwierdzić pliku na NFS: {relative}")
            self._ensure_file_readable(final_path)

        shutil.rmtree(staging)
        return year_root


def create_app():
    data_root = Path(os.environ.get("DATA_DIR", "/data"))
    upload_temp_root = data_root / ".upload-tmp"
    upload_temp_root.mkdir(parents=True, exist_ok=True)
    # Werkzeug spools multipart file bodies to tempfile once they exceed its
    # in-memory threshold. Keep those files on the persistent data volume rather
    # than the container's size-limited /tmp tmpfs.
    os.environ["TMPDIR"] = str(upload_temp_root)
    tempfile.tempdir = str(upload_temp_root)

    # Remove temporary parser files left behind by an interrupted container.
    for stale_temp in upload_temp_root.iterdir():
        if stale_temp.is_dir() and not stale_temp.is_symlink():
            shutil.rmtree(stale_temp, ignore_errors=True)
        else:
            stale_temp.unlink(missing_ok=True)

    app = Flask(__name__, static_folder="static", template_folder="templates")
    nfs_root = Path(os.environ.get("NFS_DIR", "/mnt/nfs"))
    require_nfs = os.environ.get("REQUIRE_NFS", "true").lower() in ("1", "true", "yes", "on")
    app_token = os.environ.get("APP_TOKEN", "")
    if not app_token:
        raise RuntimeError("Ustaw APP_TOKEN w pliku .env przed uruchomieniem aplikacji.")
    if not os.environ.get("DB_PASSWORD"):
        raise RuntimeError("Ustaw POSTGRES_PASSWORD w pliku .env przed uruchomieniem aplikacji.")
    initialize_patient_database()

    jobs_root = data_root / "jobs"
    incoming_root = data_root / ".incoming"
    jobs_root.mkdir(parents=True, exist_ok=True)
    incoming_root.mkdir(parents=True, exist_ok=True)

    # A request interrupted before it becomes a queued job is not accepted data.
    # Clear such partial uploads on process start; committed jobs live in jobs/.
    for abandoned_upload in incoming_root.iterdir():
        if abandoned_upload.is_dir():
            shutil.rmtree(abandoned_upload, ignore_errors=True)
        else:
            abandoned_upload.unlink(missing_ok=True)

    max_upload_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024 * 1024)))
    app.config["MAX_CONTENT_LENGTH"] = max_upload_bytes
    app.config["MAX_FORM_MEMORY_SIZE"] = 64 * 1024 * 1024
    app.config["MAX_FORM_PARTS"] = 20001

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        return response

    @app.before_request
    def require_api_token():
        if request.path.startswith("/api/") and request.path != "/api/health":
            provided = request.headers.get("X-App-Token", "")
            if not hmac.compare_digest(provided, app_token):
                return jsonify({"error": "Nieprawidłowy token dostępu."}), 401

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "nfs": nfs_monitor.snapshot()})

    @app.post("/api/patients/search")
    def find_patient():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {}
        patient_id = str(body.get("pesel", "")).strip()
        if not valid_pesel(patient_id):
            return jsonify({"error": "Wpisz prawidłowy PESEL przed wyszukaniem."}), 400
        try:
            with database_connection() as connection:
                patient = connection.execute(
                    "SELECT first_name, last_name FROM patients WHERE patient_id = %s",
                    (patient_id,),
                ).fetchone()
        except psycopg.Error:
            logger.exception("Nie udało się wyszukać pacjenta w PostgreSQL.")
            return jsonify({"error": "Baza pacjentów jest chwilowo niedostępna."}), 503
        if not patient:
            response = jsonify({"patient": None})
            response.headers["Cache-Control"] = "no-store"
            return response
        response = jsonify({
            "patient": {
                "first_name": patient["first_name"],
                "last_name": patient["last_name"],
            }
        })
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/jobs")
    def list_jobs():
        jobs = []
        for job_dir in jobs_root.iterdir():
            path = job_dir / "job.json"
            if not path.is_file():
                continue
            try:
                job = read_json(path)
                jobs.append(job)
            except (OSError, ValueError, json.JSONDecodeError):
                logger.exception("Nie udało się odczytać stanu %s", path)
        jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        active_count = sum(job.get("state") == "transferring" for job in jobs)
        return jsonify({"jobs": jobs[:100], "active_count": active_count})

    @app.post("/api/upload")
    def upload():
        upload_datetime = datetime.now()
        uploaded_files = request.files.getlist("files")
        raw_paths = request.form.getlist("paths")
        if not uploaded_files:
            return jsonify({"error": "Wybierz co najmniej jeden plik."}), 400
        if raw_paths and len(raw_paths) != len(uploaded_files):
            return jsonify({"error": "Nieprawidłowa lista ścieżek plików."}), 400
        if not raw_paths:
            raw_paths = [file.filename or "plik" for file in uploaded_files]
        if len(uploaded_files) > 10000:
            return jsonify({"error": "Paczka zawiera zbyt wiele plików (limit: 10 000)."}), 400

        try:
            relative_paths = [normalized_relative_path(value) for value in raw_paths]
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        input_path_set = {tuple(PurePosixPath(value).parts) for value in relative_paths}
        if len(input_path_set) != len(relative_paths):
            return jsonify({"error": "Paczka zawiera powtórzone ścieżki plików."}), 400

        prepared_files = []
        patient_contexts = {}
        series_contexts = {}
        patient_instance_counts = {}
        try:
            has_convertible_files = any(
                PurePosixPath(relative).suffix.lower() in {*IMAGE_FORMATS, *DOCUMENT_FORMATS}
                for relative in relative_paths
            )
            has_patient_data = bool(request.form.get("patient_id", "").strip())
            case_metadata = patient_metadata_from_form(request.form) if has_convertible_files or has_patient_data else None
            if case_metadata:
                try:
                    patient = get_or_create_patient(
                        case_metadata["patient_id"],
                        case_metadata["given_name"],
                        case_metadata["family_name"],
                    )
                except psycopg.Error:
                    logger.exception("Nie udało się zapisać pacjenta w PostgreSQL.")
                    return jsonify({"error": "Baza pacjentów jest chwilowo niedostępna. Paczka nie została przyjęta."}), 503
                case_metadata["given_name"] = patient["first_name"]
                case_metadata["family_name"] = patient["last_name"]
                case_metadata["patient_name"] = f'{patient["last_name"]}^{patient["first_name"]}'

            for index, relative in enumerate(relative_paths):
                metadata = conversion_metadata_for_file(relative, case_metadata) if case_metadata else None
                file_timestamp = (upload_datetime + timedelta(microseconds=index)).strftime("%Y%m%dT%H%M%S%f")
                output_path = dicom_output_path(relative, metadata, file_timestamp) if metadata else relative
                try:
                    output_path = normalized_relative_path(output_path)
                except ValueError:
                    raise UploadValidationError("Wygenerowana ścieżka pliku DICOM jest zbyt długa.") from None
                prepared = {"input_path": relative, "output_path": output_path, "metadata": metadata}
                if metadata:
                    patient_id = metadata["patient_id"]
                    context = patient_contexts.get(patient_id)
                    if context and context["patient_name"] != metadata["patient_name"]:
                        raise UploadValidationError("Pliki z tym samym PESEL muszą zawierać takie samo imię i nazwisko.")
                    if not context:
                        context = {
                            "patient_name": metadata["patient_name"],
                            "study_uid": generate_uid(),
                            "study_datetime": metadata["study_datetime"],
                        }
                        patient_contexts[patient_id] = context
                    series_key = (patient_id, metadata["kind"], metadata["modality"])
                    if series_key not in series_contexts:
                        series_number = 1 + sum(key[0] == patient_id for key in series_contexts)
                        series_contexts[series_key] = {
                            "series_uid": generate_uid(),
                            "series_number": series_number,
                        }
                    series_context = series_contexts[series_key]
                    instance_key = series_key
                    patient_instance_counts[instance_key] = patient_instance_counts.get(instance_key, 0) + 1
                    prepared["study_uid"] = context["study_uid"]
                    prepared["series_uid"] = series_context["series_uid"]
                    prepared["series_number"] = series_context["series_number"]
                    prepared["study_datetime"] = context["study_datetime"]
                    prepared["instance_number"] = patient_instance_counts[instance_key]
                prepared_files.append(prepared)
        except UploadValidationError as error:
            return jsonify({"error": str(error)}), 400

        output_path_set = {tuple(PurePosixPath(item["output_path"]).parts) for item in prepared_files}
        if len(output_path_set) != len(prepared_files):
            return jsonify({"error": "Po konwersji pliki miałyby powtarzające się ścieżki docelowe."}), 400
        for path_set in (input_path_set, output_path_set):
            for parts in path_set:
                if any(parts[:index] in path_set for index in range(1, len(parts))):
                    return jsonify({"error": "Ścieżka pliku koliduje z katalogiem w paczce."}), 400

        job_id = uuid.uuid4().hex
        job_dir = incoming_root / job_id
        payload_dir = job_dir / "payload"
        source_dir = job_dir / "sources"
        payload_dir.mkdir(parents=True, exist_ok=False)
        converted_images = sum(bool(item["metadata"] and item["metadata"]["kind"] == "image") for item in prepared_files)
        converted_documents = sum(bool(item["metadata"] and item["metadata"]["kind"] == "pdf") for item in prepared_files)
        stored_files = []
        total_bytes = 0
        try:
            for uploaded, prepared in zip(uploaded_files, prepared_files):
                original_relative = prepared["input_path"]
                output_relative = prepared["output_path"]
                metadata = prepared["metadata"]
                output_path = payload_dir.joinpath(*PurePosixPath(output_relative).parts)
                output_path.parent.mkdir(parents=True, exist_ok=True)

                if metadata:
                    source_path = source_dir.joinpath(*PurePosixPath(original_relative).parts)
                    source_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with source_path.open("xb") as output:
                        os.fchmod(output.fileno(), 0o600)
                        while True:
                            chunk = uploaded.stream.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    converter = convert_pdf_to_dicom if metadata["kind"] == "pdf" else convert_image_to_dicom
                    converter(
                        source_path,
                        output_path,
                        metadata,
                        prepared["study_uid"],
                        prepared["series_uid"],
                        prepared["series_number"],
                        prepared["instance_number"],
                        prepared["study_datetime"],
                    )
                else:
                    with output_path.open("wb") as output:
                        while True:
                            chunk = uploaded.stream.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())

                digest = hashlib.sha256()
                size = 0
                with output_path.open("rb") as saved_file:
                    while True:
                        chunk = saved_file.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                total_bytes += size
                stored_files.append({"path": output_relative, "size": size, "sha256": digest.hexdigest()})

            job = {
                "id": job_id,
                "name": PurePosixPath(relative_paths[0]).parts[0],
                "state": "queued",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "attempts": 0,
                "error": None,
                "next_attempt_at": None,
                "destination": None,
                "total_bytes": total_bytes,
                "converted_images": converted_images,
                "converted_documents": converted_documents,
                "files": stored_files,
            }
            atomic_write_json(job_dir / "job.json", job)
            final_job_dir = jobs_root / job_id
            os.replace(job_dir, final_job_dir)
            return jsonify({"job": job}), 201
        except UploadValidationError as error:
            shutil.rmtree(job_dir, ignore_errors=True)
            return jsonify({"error": str(error)}), 400
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

    @app.post("/api/jobs/<job_id>/retry")
    def retry_job(job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            return jsonify({"error": "Nie znaleziono paczki."}), 404
        job_dir = jobs_root / job_id
        job_path = job_dir / "job.json"
        if not job_path.is_file():
            return jsonify({"error": "Nie znaleziono paczki."}), 404
        try:
            job = read_json(job_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return jsonify({"error": "Nie udało się odczytać paczki."}), 500
        if job.get("state") == "delivered":
            return jsonify({"error": "Ta paczka została już wysłana."}), 409
        job["state"] = "queued"
        job["next_attempt_at"] = None
        job["error"] = None
        job["updated_at"] = utc_now()
        atomic_write_json(job_path, job)
        return jsonify({"job": job})

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        limit_gb = max_upload_bytes / (1024 ** 3)
        return jsonify({"error": f"Przekroczono limit rozmiaru uploadu ({limit_gb:g} GB)."}), 413

    nfs_monitor = NfsMonitor(nfs_root)
    nfs_monitor.start()
    app.extensions["nfs_monitor"] = nfs_monitor

    worker = TransferWorker(data_root, nfs_root, require_nfs)
    worker.start()
    app.extensions["transfer_worker"] = worker
    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
