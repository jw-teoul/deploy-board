"""수집 결과(data/state.json)를 공유용 보드 서버로 올린다.

실행: python publish.py <대상>        예) python publish.py datacrawler:deploy-board/data/state.json
대상이 `호스트:경로` 면 ssh 한 번으로 임시 파일에 쓰고 mv 로 바꿔 끼운다(중간에 끊겨도 반쪽 파일이 안 남는다).
로컬 경로면 복사 후 os.replace. server.py --publish <대상> 은 수집이 끝날 때마다 이 함수를 부른다.
공유 서버에는 토큰을 두지 않는다 — 이 PC 의 SSH 키로 파일만 넣는다.
"""
import os
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "data", "state.json")


def is_remote(target):
    if re.match(r"^[A-Za-z]:[\\/]", target):  # Windows 드라이브 경로
        return False
    head = target.split(":", 1)[0]
    return ":" in target and "/" not in head and "\\" not in head


def publish(target, src=STATE, timeout=60):
    """성공하면 None, 실패하면 오류 문자열."""
    if not os.path.exists(src):
        return f"올릴 파일 없음: {src}"
    try:
        # 파일은 읽는 순간만 연다 — 오래 걸리는 ssh 동안 열어 두면 Windows 에서 수집 쪽 os.replace(state.json) 가 막힌다
        with open(src, "rb") as f:
            data = f.read()
        if is_remote(target):
            host, path = target.split(":", 1)
            d, tmp = os.path.dirname(path) or ".", path + ".tmp"
            remote = f"mkdir -p {shlex.quote(d)} && cat > {shlex.quote(tmp)} && mv {shlex.quote(tmp)} {shlex.quote(path)}"
            p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, remote],
                               input=data, capture_output=True, timeout=timeout)
            if p.returncode != 0:
                return f"ssh rc={p.returncode}: {p.stderr.decode('utf-8', 'replace')[-300:]}"
        else:
            os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
            tmp = target + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
    except Exception as e:
        return str(e)[-300:]
    return None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    err = publish(sys.argv[1])
    print(err or f"올림: {sys.argv[1]}")
    sys.exit(1 if err else 0)
