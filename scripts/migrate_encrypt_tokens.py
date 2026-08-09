import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import InvalidToken  # noqa: E402

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import User  # noqa: E402
from app.services.token_crypto import decrypt_token, encrypt_token  # noqa: E402


def main():
    db = SessionLocal()
    users = db.query(User).filter(User.google_refresh_token.isnot(None)).all()

    encrypted = 0
    already_encrypted = 0

    for user in users:
        try:
            decrypt_token(user.google_refresh_token)
            already_encrypted += 1
            print(f"User {user.id}: already encrypted, skipping")
        except InvalidToken:
            user.google_refresh_token = encrypt_token(user.google_refresh_token)
            encrypted += 1
            print(f"User {user.id}: encrypted plaintext token")

    db.commit()
    db.close()
    print(f"\nDone. Encrypted: {encrypted}, already encrypted: {already_encrypted}")


if __name__ == "__main__":
    main()
