import os
import re
from pathlib import Path


def split_translation_file(input_file, output_dir, max_blocks=60):
    # 使用正则表达式匹配代码块
    block_pattern = re.compile(r'(# game/.*?translate.*?\n.*?)(?=# game/|\Z)', re.DOTALL)

    # 读取输入文件
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # 查找所有匹配的代码块
    blocks = block_pattern.findall(content)

    # 创建输出目录
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # 分割并写入文件
    for i in range(0, len(blocks), max_blocks):
        chunk = blocks[i:i + max_blocks]
        output_file = os.path.join(output_dir, f'split_{i // max_blocks + 1}.rpy')

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(''.join(chunk))


if __name__ == '__main__':
    input_file = input("请输入输入文件名（按回车使用当前目录下所有.rpy文件）：").strip()
    output_dir = 'split_files'  # 输出目录

    if not input_file:
        # 获取当前目录下所有的 .rpy 文件
        rpy_files = [f.name for f in Path('.').glob('*.rpy')]

        if not rpy_files:
            print("当前目录下没有找到任何 .rpy 文件。")
        else:
            for file_name in rpy_files:
                print(f"正在处理文件: {file_name}")
                split_translation_file(file_name, output_dir)
    else:
        split_translation_file(input_file, output_dir)