from cryptography.fernet import Fernet

from app.config import settings

# Fernet = symmetric authenticated encryption (AES-128-CBC + HMAC-SHA256),
# URL-safe base64 output. Authenticated means tampering is detectable:
# decrypting a modified or non-Fernet string raises InvalidToken rather
# than silently returning garbage — this also lets us tell encrypted
# tokens apart from old plaintext ones during migration.
_fernet = Fernet(settings.token_encryption_key.encode())


def encrypt_token(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()
