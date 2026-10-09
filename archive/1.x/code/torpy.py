import os
import csv


def clean_dialogue(dialogue: str) -> str:
    """清理台词中的英文引号（保留转义符已由csv模块处理）"""
    return dialogue.replace('"', '')


def torpy():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(script_dir, "output")

    # 确保output目录存在
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # 遍历output目录中的所有CSV文件
    for csv_file in os.listdir(output_dir):
        if csv_file.endswith('.csv'):
            base_name = os.path.splitext(csv_file)[0]
            rpy_file = os.path.join(output_dir, f"{base_name}.rpy")

            if not os.path.exists(rpy_file):
                print(f"正在生成 {rpy_file}")
                with open(rpy_file, 'w', encoding='utf-8') as f:
                    pass  # 创建空文件

                csv_path = os.path.join(output_dir, csv_file)
                try:
                    with open(csv_path, 'r', encoding='utf-8') as f:
                        csv_reader = csv.reader(f)
                        csv_rows = list(csv_reader)
                except Exception as e:
                    print(f"读取文件 {csv_file} 时出错: {str(e)}")
                    continue

                rpy_content = []
                added_translate = False  # 标记是否已添加"translate chinese strings:"

                for row in csv_rows:
                    # 过滤无效行：行长度≥5且定位符以#开头
                    if len(row) >= 5 and row[0].startswith('#'):
                        line_number = row[0]  # 定位符（如# game/script.rpy:73）
                        feature_code = row[1]  # 特征码（如old "Skip it"）

                        if feature_code.startswith('old'):
                            # 处理old特征逻辑
                            raw_dialogue = row[4]  # 台词列（用于new行）
                            cleaned_dialogue = clean_dialogue(raw_dialogue)

                            # 首次遇到old特征时添加翻译声明
                            if not added_translate:
                                rpy_content.append("translate chinese strings:")
                                added_translate = True

                            # 缩进4空格，添加定位符行（顶格→缩进）
                            rpy_content.append(f"    {line_number}")
                            # 缩进4空格，添加old行（直接使用特征码列内容）
                            rpy_content.append(f"    {feature_code}")
                            # 缩进4空格，添加new行（清理后的台词）
                            rpy_content.append(f'    new "{cleaned_dialogue}"')
                            # 分隔空行（不同代码段间）
                            rpy_content.append("")

                        else:
                            # 处理非old特征逻辑（保持原格式）
                            raw_dialogue = row[4]
                            cleaned_dialogue = clean_dialogue(raw_dialogue)

                            # 定位符行顶格
                            rpy_content.append(f"{line_number}")
                            # 翻译声明行
                            rpy_content.append(f"{feature_code}")
                            # 空行分隔
                            rpy_content.append("")
                            # 原文注释（如果有，缩进4空格）
                            if row[2]:
                                rpy_content.append(f"    {row[2]}")
                            # 台词行（角色名或直接台词，缩进4空格）
                            role_name = row[3]
                            if role_name:
                                rpy_content.append(f"    {role_name} \"{cleaned_dialogue}\"")
                            else:
                                rpy_content.append(f"    \"{cleaned_dialogue}\"")
                            # 分隔空行
                            rpy_content.append("")

                # 去除末尾多余的空行
                if rpy_content:
                    rpy_content.pop()

                # 写入.rpy文件
                try:
                    with open(rpy_file, 'w', encoding='utf-8', newline='') as f:
                        f.write("\n".join(rpy_content))
                    print(f"已生成 {rpy_file}")
                except Exception as e:
                    print(f"写入文件 {rpy_file} 时出错: {str(e)}")
            else:
                print(f"文件 {csv_file} 对应的.rpy文件已存在，跳过生成")


if __name__ == "__main__":
    torpy()
