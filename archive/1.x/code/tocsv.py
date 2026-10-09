import os
import csv
import re


def parse_rpy_to_csv():
    # 获取用户输入
    rpy_filename = input("请输入rpy文件名（直接回车处理当前目录下所有rpy文件）: ").strip()

    # 如果用户直接回车，则处理所有rpy文件
    if not rpy_filename:
        rpy_files = [f for f in os.listdir() if f.endswith('.rpy')]
    else:
        # 检查文件是否存在
        if not os.path.exists(rpy_filename):
            print(f"错误: 文件 {rpy_filename} 不存在!")
            return

        rpy_files = [rpy_filename]

    # 确保csv目录存在
    csv_dir = "csv"
    if not os.path.exists(csv_dir):
        os.makedirs(csv_dir)

    # 处理每个rpy文件
    for rpy_file in rpy_files:
        print(f"正在处理文件: {rpy_file}")
        results = []

        with open(rpy_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # 寻找所有代码块
        i = 0
        while i < len(lines):
            line = lines[i].strip()

            # 1. 定位到第一个#game
            if line.startswith("# game/"):
                # 记录定位符
                marker_line = line

                # 检查下一行是否以old开头
                if i + 1 < len(lines) and lines[i + 1].strip().startswith("old"):
                    # 处理old开头的代码块
                    i += 1  # 移动到old行
                    old_line = lines[i].strip()
                    # 提取old后面的引号内容
                    old_match = re.search(r'old\s+"([^"]+)"', old_line)
                    if old_match:
                        old_content = old_match.group(1)
                    else:
                        old_content = old_line.replace("old", "").strip()

                    # 找到new行
                    i += 1
                    while i < len(lines) and lines[i].strip() == "":
                        i += 1

                    if i < len(lines):
                        new_line = lines[i].strip()
                        # 提取new后面的引号内容
                        new_match = re.search(r'new\s+"([^"]+)"', new_line)
                        if new_match:
                            new_content = new_match.group(1)
                        else:
                            new_content = new_line.replace("new", "").strip()

                        # 创建数据字典
                        code_block = {
                            "定位符": marker_line,
                            "特征码": old_line,
                            "原文注释": old_content,
                            "角色名": "new",
                            "台词": new_content
                        }
                        results.append(code_block)
                else:
                    # 处理普通代码块
                    i += 1  # 移动到下一行
                    while i < len(lines) and lines[i].strip() == "":
                        i += 1

                    if i < len(lines):
                        feature_line = lines[i].strip()

                        # 从特征码行向下一行（忽略空行），作为原文填入
                        i += 1
                        while i < len(lines) and lines[i].strip() == "":
                            i += 1

                        if i < len(lines):
                            dialogue_line = lines[i].strip()

                            # 从原文行向下一行（忽略空行），提取角色和台词
                            i += 1
                            while i < len(lines) and lines[i].strip() == "":
                                i += 1

                            if i < len(lines):
                                dialogue_text = lines[i].strip()

                                # 提取角色名和台词
                                # 角色名是引号前的部分，台词是引号内的部分
                                if '"' in dialogue_text:
                                    role_part, dialogue_part = dialogue_text.split('"', 1)
                                    role_name = role_part.replace('    ', '').strip()
                                    # 去除引号
                                    dialogue = dialogue_part.strip('"').strip()
                                else:
                                    # 如果没有引号，整个行作为台词，角色名为空
                                    role_name = ""
                                    dialogue = dialogue_text.strip()

                                # 创建数据字典
                                code_block = {
                                    "定位符": marker_line,
                                    "特征码": feature_line,
                                    "原文注释": dialogue_line,
                                    "角色名": role_name,
                                    "台词": dialogue
                                }
                                results.append(code_block)

                # 移动到下一个代码块的开始位置
                i += 1

            else:
                i += 1

        # 创建CSV文件
        csv_filename = os.path.join(csv_dir, os.path.basename(rpy_file).replace('.rpy', '.csv'))
        with open(csv_filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["定位符", "特征码", "原文注释", "角色名", "台词"])
            for row in results:
                writer.writerow([
                    row["定位符"],
                    row["特征码"],
                    row["原文注释"],
                    row["角色名"],
                    row["台词"]
                ])

        print(f"文件处理完成: {rpy_file}")
        print(f"已生成CSV文件: {csv_filename}")
        print(f"共处理 {len(results)} 条记录")


if __name__ == "__main__":
    parse_rpy_to_csv()
