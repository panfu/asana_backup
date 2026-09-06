"""生成 SECRET_KEY：python -m app.genkey"""

import secrets


def main() -> None:
    print(secrets.token_urlsafe(32))


if __name__ == "__main__":
    main()
