import os
import csv
import sys
import shutil


def split_csv_files():
    # 获取要处理的CSV文件
    csv_dir = "csv"
    csv_files = []

    # 询问用户要处理的文件名
    print("请输入要处理的CSV文件名:")
    filename = input("文件名(输入回车表示处理所有文件): ").strip()

    # 如果用户输入了文件名
    if filename:
        if os.path.exists(os.path.join(csv_dir, filename)):
            csv_files = [filename]
            print(f"将处理文件: {filename}")
        else:
            print(f"错误: 文件 {filename} 不存在于csv目录中!")
            return
    else:
        # 获取csv目录中的所有CSV文件
        for file in os.listdir(csv_dir):
            if file.endswith('.csv'):
                csv_files.append(file)
        print(f"将处理目录中的所有CSV文件，共 {len(csv_files)} 个")

    # 确保split_csv目录存在
    split_dir = "split_csv"
    if not os.path.exists(split_dir):
        os.makedirs(split_dir)

    # 确保csvbak目录存在
    csvbak_dir = "csvbak"
    if not os.path.exists(csvbak_dir):
        os.makedirs(csvbak_dir)

    # 处理每个CSV文件
    for csv_file in csv_files:
        file_path = os.path.join(csv_dir, csv_file)
        print(f"\n正在处理文件: {csv_file}")

        # 读取CSV文件内容
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            rows = list(reader)
            total_rows = len(rows)
            print(f"文件共有 {total_rows} 行数据")

            # 计算需要分割的文件数量
            num_parts = (total_rows + 29) // 30  # 向上取整
            print(f"需要分割成 {num_parts} 个文件")

            # 分割CSV文件
            for i in range(num_parts):
                start_row = i * 30 + 1  # 跳过表头
                end_row = min(start_row + 29, total_rows)

                # 创建输出文件名
                base_name = os.path.basename(csv_file).replace('.csv', '')
                output_filename = os.path.join(split_dir, f"{base_name}_{i + 1:03d}.csv")

                # 写入分割后的CSV文件 - 只保留定位符、特征码、台词三列
                with open(output_filename, 'w', newline='', encoding='utf-8') as out_file:
                    writer = csv.writer(out_file)
                    # 写入表头 - 只保留需要的三列
                    writer.writerow([rows[0][0], rows[0][1], rows[0][4]])  # 定位符、特征码、台词

                    # 写入数据行 - 只保留需要的三列
                    for row in rows[start_row:end_row]:
                        # 确保行有足够的列数
                        if len(row) >= 5:  # 至少需要5列才能获取台词
                            writer.writerow([row[0], row[1], row[4]])
                        else:
                            # 如果行不够，跳过该列
                            continue

                print(f"已创建文件: {output_filename} (行 {start_row + 1}-{end_row})")

            # 移动原文件到csvbak目录
            bak_file_path = os.path.join(csvbak_dir, csv_file)
            try:
                shutil.move(file_path, bak_file_path)
                print(f"已将原文件移动到备份目录: {bak_file_path}")
            except Exception as e:
                print(f"移动文件时出错: {str(e)}")
                print("原文件未移动，请手动备份原文件")

        print("-" * 50)


if __name__ == "__main__":
    split_csv_files()
