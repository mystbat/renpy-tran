import os
import csv
import re


def check_csv_errors():
    # 获取当前脚本的目录
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # 构建split_csv目录的路径
    csv_dir = os.path.join(script_dir, "split_csv")

    # 检查split_csv目录是否存在
    if not os.path.exists(csv_dir):
        print(f"错误: {csv_dir} 目录不存在!")
        return

    # 存储错误的列表
    errors = []

    # 获取split_csv目录中所有-s.csv文件
    csv_files = [f for f in os.listdir(csv_dir) if f.endswith('-s.csv')]

    if not csv_files:
        print(f"在{csv_dir}目录中没有找到-s.csv文件!")
        return

    print(f"找到 {len(csv_files)} 个-s.csv文件需要检查")

    # 处理每个CSV文件
    for csv_file in csv_files:
        # 构建当前CSV文件的路径
        csv_path = os.path.join(csv_dir, csv_file)

        # 获取对应的原CSV文件名（去掉-s后缀）
        base_name = os.path.splitext(csv_file)[0]
        original_csv = base_name.replace('-s', '') + '.csv'
        original_csv_path = os.path.join(csv_dir, original_csv)

        # 检查原CSV文件是否存在
        if not os.path.exists(original_csv_path):
            print(f"警告: 原文件 {original_csv} 不存在，跳过检查")
            continue

        # 读取原CSV文件内容
        try:
            with open(original_csv_path, 'r', encoding='utf-8') as f:
                original_reader = csv.reader(f)
                original_rows = list(original_reader)
        except Exception as e:
            print(f"读取原文件 {original_csv} 时出错: {str(e)}")
            continue

        # 读取当前CSV文件内容
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                csv_reader = csv.reader(f)
                csv_rows = list(csv_reader)
        except Exception as e:
            print(f"读取文件 {csv_file} 时出错: {str(e)}")
            continue

        # 检查特征码是否一致
        original_features = []
        csv_features = []

        # 提取原CSV文件的特征码
        for i, row in enumerate(original_rows):
            if i == 0:  # 跳过标题行
                continue
            if len(row) > 1:  # 确保有特征码列
                original_features.append(row[1])  # 特征码在第二列（索引1）

        # 提取*-s.csv文件的特征码
        for i, row in enumerate(csv_rows):
            if i == 0:  # 跳过标题行
                continue
            if len(row) > 1:  # 确保有特征码列
                csv_features.append(row[1])  # 特征码在第二列（索引1）

        # 检查每个特征码是否在*-s.csv文件中存在
        for feature in original_features:
            if feature not in csv_features:
                errors.append({
                    "文件名": csv_file,
                    "行号": original_features.index(feature) + 2,  # 行号从1开始，跳过标题行
                    "错误类型": "特征码缺失",
                    "错误内容": f"特征码 '{feature}' 在处理后的文件中不存在"
                })

        # 检查台词是否包含英文字母和英文引号
        for i in range(1, len(csv_rows)):  # 从第2行开始（跳过标题行）
            if len(csv_rows) <= i:
                break

            csv_row = csv_rows[i]
            # 检查台词列（假设第三列是台词）
            if len(csv_row) > 2 and csv_row[2]:
                # 检查是否包含英文字母或英文引号
                if re.search(r'[a-zA-Z"]', csv_row[2]):
                    errors.append({
                        "文件名": csv_file,
                        "行号": i + 1,  # 行号从1开始
                        "错误类型": "台词包含英文字母或引号",
                        "错误内容": f"行 {i + 1}: '{csv_row[2]}'"
                    })

    # 按文件名排序错误
    errors.sort(key=lambda x: x["文件名"])

    # 生成HTML报告
    if errors:
        generate_html_report(errors, csv_dir)
        print(f"共发现 {len(errors)} 处错误，已生成报告: check-csv.html")
    else:
        print("未发现错误")


def generate_html_report(errors, csv_dir):
    # 创建HTML文件
    html_path = os.path.join(csv_dir, "check-csv.html")

    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(
            "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"UTF-8\">\n<title>CSV文件检查报告</title>\n<style>\n    body { font-family: Arial, sans-serif; margin: 20px; }\n    h1 { color: #333; }\n    table { border-collapse: collapse; width: 100%; margin-top: 20px; }\n    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }\n    th { background-color: #f2f2f2; }\n    tr:nth-child(even) { background-color: #f9f9f9; }\n    .error { color: red; }\n</style>\n</head>\n<body>\n    <h1>CSV文件检查报告</h1>\n    <p>共发现 {len(errors)} 处错误</p>\n    <table>\n        <tr>\n            <th>文件名</th>\n            <th>行号</th>\n            <th>错误类型</th>\n            <th>错误详情</th>\n        </tr>")

        # 写入错误记录
        for error in errors:
            f.write(
                f"<tr>\n    <td>{error['文件名']}</td>\n    <td>{error['行号']}</td>\n    <td>{error['错误类型']}</td>\n    <td class=\"error\">{error['错误内容']}</td>\n</tr>")

        f.write("</table>\n</body>\n</html>")

    print(f"HTML报告已生成: {html_path}")


if __name__ == "__main__":
    check_csv_errors()
