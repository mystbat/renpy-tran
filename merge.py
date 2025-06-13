import os
import re


def merge_files():
    # 获取当前脚本的目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    solved_dir = os.path.join(script_dir, "solved")
    text_dir = os.path.join(script_dir, "text")  # 修改为text目录

    # 检查solved目录是否存在
    if not os.path.exists(solved_dir):
        print(f"错误: {solved_dir} 目录不存在!")
        return

    # 获取所有txt文件
    txt_files = [f for f in os.listdir(solved_dir) if f.endswith('.txt')]

    if not txt_files:
        print(f"在{solved_dir}目录中没有找到txt文件!")
        return

    print(f"找到 {len(txt_files)} 个txt文件需要合并")

    # 提取文件名中的序号，并排序
    def extract_number(filename):
        # 匹配模式：*_split_数字.txt
        match = re.search(r'(.*)_(split|part)_(\d+)\.txt', filename)
        if not match:
            return None
        base_name = match.group(1)
        number = match.group(3)
        # 返回一个元组，包含基础名称和序号，以便排序
        return base_name, int(number)

    # 过滤出符合命名规则的文件（包含_split_和数字）
    pattern_files = []
    for f in txt_files:
        # 检查文件名是否符合模式：*_split_数字.txt
        if re.search(r'_(split|part)_(\d+)\.txt', f):
            pattern_files.append(f)

    if not pattern_files:
        print("错误: 未找到符合命名规则的txt file!")
        print("符合规则的文件名应该类似: 'xxx_split_001.txt'")
        return

    # 按照提取的序号排序
    pattern_files = sorted(pattern_files, key=lambda f: extract_number(f))

    # 确保text目录存在
    if not os.path.exists(text_dir):
        os.makedirs(text_dir)

    # 提取基础名称（去掉_split_序号.txt部分）
    def get_base_name(filename):
        match = re.search(r'(.*)_(split|part)_(\d+)\.txt', filename)
        if match:
            return match.group(1)
        return filename

    # 构建输出文件名，去掉_split_序号部分，保留基础名称和扩展名（.txt改为.rpy)
    base_name = get_base_name(pattern_files[0])
    output_filename = f"{base_name}.rpy"

    # 处理同名文件
    def get_unique_filename(base_path, filename):
        counter = 1
        while os.path.exists(os.path.join(base_path, filename)):
            name, ext = os.path.splitext(filename)
            new_filename = f"{name}_{counter}{ext}"
            if not os.path.exists(os.path.join(base_path, new_filename)):
                return new_filename
            counter += 1
        return filename

    # 确保输出文件名唯一
    output_filename = get_unique_filename(text_dir, output_filename)

    # 写入合并后的内容
    try:
        with open(os.path.join(text_dir, output_filename), 'w', encoding='utf-8') as output_file:
            for file_name in pattern_files:
                # 写入文件名注释
                output_file.write(f"# {file_name}\n")

                # 读取文件内容并写入
                with open(os.path.join(solved_dir, file_name), 'r', encoding='utf-8') as f:
                    content = f.read()
                    output_file.write(content)
                    output_file.write('\n\n')  # 在每个文件内容后添加空行

        print(f"合并完成，输出文件: {output_filename}")
    except Exception as e:
        print(f"合并文件时出错: {str(e)}")


if __name__ == "__main__":
    merge_files()
