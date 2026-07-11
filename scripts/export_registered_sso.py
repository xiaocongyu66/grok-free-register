#!/usr/bin/env python3
"""以 SSH 管道可消费的形式导出注册账号的邮箱与 SSO。

输入格式为 ``email:password:sso``；密码仅用于定位分隔符，绝不写入输出。
"""

import argparse
from pathlib import Path


def export_accounts(path):
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        try:
            email, _password, sso = line.rsplit(":", 2)
        except ValueError as error:
            raise ValueError(f"invalid account record at line {line_number}") from error
        if not email or not sso or email in seen:
            continue
        seen.add(email)
        yield email, sso


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("accounts_path", type=Path)
    args = parser.parse_args()
    for email, sso in export_accounts(args.accounts_path):
        print(f"{email}\t{sso}")


if __name__ == "__main__":
    main()
