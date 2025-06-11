import os
import re
from pathlib import Path
import configparser


def split_translation_file(input_file, output_dir, max_blocks=60):
    """使用正则表达式匹配代码块并分割文件"""
    # 使用正则表达式匹配代码块
    block_pattern = re.compile(r'(# game/.*?translate.*?\n.*?)(?=# game/|\Z)', re.DOTALL)

    # 读取输入文件
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # 查找所有匹配的代码块
    blocks = block_pattern.findall(content)

    # 创建输出目录
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # 获取文件名（不含扩展名）
    filename_without_ext = os.path.splitext(os.path.basename(input_file))[0]

    # 分割并写入文件
    for i in range(0, len(blocks), max_blocks):
        chunk = blocks[i:i + max_blocks]
        # 使用原文件名和序号创建输出文件名
        output_file = os.path.join(output_dir, f"{filename_without_ext}_split_{i // max_blocks + 1:03d}.rpy")

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(''.join(chunk))


def main():
    # 读取配置文件
    config = configparser.ConfigParser()
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')

    if not os.path.exists(config_path):
        # 如果配置文件不存在，创建一个默认配置
        config.add_section('DEFAULT')
        config.set('DEFAULT', 'OutputDir', 'split_files')
        config.set('DEFAULT', 'BlockSize', '60')

        with open(config_path, 'w', encoding='utf-8') as configfile:
            config.write(configfile)
        logging.info(f"已创建默认配置文件: {config_path}")

    try:
        config.read(config_path)
    except configparser.Error as e:
        logging.error(f"配置文件解析错误: {str(e)}")
        return

    # 获取配置
    try:
        output_dir = config['DEFAULT'].get('OutputDir', 'split_files')
        block_size = int(config['DEFAULT'].get('BlockSize', '60'))
    except KeyError as e:
        logging.error(f"配置文件缺少必要字段: {e}")
        return

    # 获取用户输入
    input_file = input("请输入输入文件名（按回车使用当前目录下所有.rpy文件）：").strip()
    output_dir = os.path.abspath(output_dir)  # 使用绝对路径

    if not input_file:
        # 获取当前目录下所有的 .rpy 文件
        rpy_files = [f.name for f in Path('.').glob('*.rpy')]

        if not rpy_files:
            print("当前目录下没有找到任何 .rpy 文件。")
            return
        else:
            for file_name in rpy_files:
                print(f"正在处理文件: {file_name}")
                split_translation_file(file_name, output_dir, block_size)
    else:
        split_translation_file(input_file, output_dir, block_size)


if __name__ == '__main__':
    # 设置日志记录
    import logging

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    main()
