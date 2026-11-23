"""Self-signed X.509 device identities for IoT Hub thumbprint authentication.

Each device gets an ECDSA P-256 key and a certificate whose CN is the device ID.
IoT Hub pins the certificate's SHA-256 thumbprint. Keys are written owner-only.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# A subset of IoT Hub's device ID characters that is also safe in file names on Windows.
DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-._]{0,127}$")
MAX_DAYS = 825


def validate_device_id(device_id: str) -> str:
    if not DEVICE_ID.fullmatch(device_id):
        raise ValueError(f"invalid IoT Hub device id {device_id!r}")
    return device_id


def thumbprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex().upper()


def create_certificate(
    device_id: str, days: int = 365
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    validate_device_id(device_id)
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)])
    now = datetime.now(UTC)
    usage = x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(usage, critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def ensure_identity(certs_dir: Path, device_id: str, days: int = 365) -> tuple[x509.Certificate, bool]:
    """Load the device certificate, creating key and certificate first if absent.

    Returns the certificate and whether it was created now.
    """
    cert_path = certs_dir / f"{device_id}.cert.pem"
    key_path = certs_dir / f"{device_id}.key.pem"
    if cert_path.exists() and key_path.exists():
        return x509.load_pem_x509_certificate(cert_path.read_bytes()), False
    if cert_path.exists() or key_path.exists():
        raise ValueError(f"{device_id}: only one of {cert_path.name} and {key_path.name} exists")
    key, cert = create_certificate(device_id, days)
    certs_dir.mkdir(parents=True, exist_ok=True)
    data = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert, True
