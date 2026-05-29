#!/usr/bin/env python3
"""
Kiwi `model_type='sbg'`용 바이너리(skipbigram.mdl, sj.knlm)를 현재 환경의
`kiwipiepy_model` 패키지 디렉터리에 복사합니다.

kiwipiepy 0.22+ 기본 배포(kiwipiepy-model)에는 CoNg만 포함되며, SBG/KnLM
파일은 제외되었습니다. 동일 런타임(예: 0.23.x)과 호환되는 조합은
공식 0.23 모델 디렉터리 + PyPI `kiwipiepy-model==0.21.0`에서 추출한
두 파일을 덧붙이는 방식입니다(로컬에서 검증됨).

사용:
  python scripts/install_kiwi_sbg_addon.py
  python scripts/install_kiwi_sbg_addon.py --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


SDIST_PKG = "kiwipiepy-model==0.21.0"
FILES = ("skipbigram.mdl", "sj.knlm")
PREFIX_IN_TAR = "kiwipiepy_model-0.21.0/kiwipiepy_model/"


def _model_dir() -> Path:
    import kiwipiepy_model

    return Path(kiwipiepy_model.__file__).resolve().parent


def _download_sdist(dest_dir: Path) -> Path:
    subprocess.run(
        [sys.executable, "-m", "pip", "download", "--no-deps", "-d", str(dest_dir), SDIST_PKG],
        check=True,
    )
    found = list(dest_dir.glob("kiwipiepy_model-0.21.0.tar.gz"))
    if not found:
        raise FileNotFoundError(f"다운로드된 sdist를 찾을 수 없습니다: {dest_dir}")
    return found[0]


def _extract_files(tar_path: Path, names: tuple[str, ...], dest: Path) -> None:
    with tarfile.open(tar_path, "r:gz") as tf:
        for name in names:
            member = PREFIX_IN_TAR + name
            info = tf.getmember(member)
            src = tf.extractfile(info)
            if src is None:
                raise RuntimeError(f"압축 해제 실패: {member}")
            data = src.read()
            out = dest / name
            out.write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Kiwi SBG 모델 파일(kiwipiepy_model에 추가)")
    parser.add_argument("--dry-run", action="store_true", help="복사만 시뮬레이션")
    args = parser.parse_args()

    target = _model_dir()
    missing = [f for f in FILES if not (target / f).exists()]
    if not missing:
        print(f"이미 설치됨: {', '.join(str(target / f) for f in FILES)}")
        return 0

    print(f"대상 디렉터리: {target}")
    print(f"추가할 파일: {', '.join(missing)}")

    if args.dry_run:
        print("--dry-run: pip download 및 복사는 수행하지 않습니다.")
        return 0

    with tempfile.TemporaryDirectory(prefix="kiwi_sbg_") as tmp:
        tmp_path = Path(tmp)
        tar = _download_sdist(tmp_path)
        _extract_files(tar, tuple(missing), tmp_path)
        for name in missing:
            shutil.copy2(tmp_path / name, target / name)
            print(f"복사 완료: {target / name}")

    from kiwipiepy import Kiwi

    Kiwi(model_type="sbg", num_workers=-1)
    print("검증: Kiwi(model_type='sbg') 초기화 성공")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
