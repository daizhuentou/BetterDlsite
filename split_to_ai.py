import shutil
from pathlib import Path

BASE_DIR = Path(__file__).parent
PENDING_TRANSLATE_DIR = BASE_DIR / "待翻译"
AI_DIR = BASE_DIR / "ai"
FILES_PER_DIR = 1000


def main():
    if not PENDING_TRANSLATE_DIR.exists():
        print(f"目录不存在: {PENDING_TRANSLATE_DIR}")
        return

    files = sorted(f for f in PENDING_TRANSLATE_DIR.iterdir() if f.is_file())
    if not files:
        print("待翻译目录中没有文件")
        return

    total = len(files)
    num_dirs = (total + FILES_PER_DIR - 1) // FILES_PER_DIR

    print(f"共 {total} 个文件，将分到 {num_dirs} 个文件夹（每个最多 {FILES_PER_DIR} 个）")

    AI_DIR.mkdir(parents=True, exist_ok=True)

    for i, src_file in enumerate(files):
        dir_index = i // FILES_PER_DIR + 1
        target_dir = AI_DIR / f"part_{dir_index}"
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, target_dir / src_file.name)

        if (i + 1) % 100 == 0 or i + 1 == total:
            print(f"  已复制 {i + 1}/{total}")

    print(f"\n完成！文件已复制到 {AI_DIR}/")
    for d in sorted(AI_DIR.iterdir()):
        if d.is_dir():
            count = sum(1 for _ in d.iterdir() if _.is_file())
            print(f"  {d.name}: {count} 个文件")


if __name__ == "__main__":
    main()
