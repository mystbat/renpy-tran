import os
import csv
import re


def csv_to_rpy():
    # 获取当前目录中的所有CSV文件
    csv_files = [f for f in os.listdir() if f.endswith('.csv')]

    # 确保text目录存在
    text_dir = "text"
    if not os.path.exists(text_dir):
        os.makedirs(text_dir)

    # 处理每个CSV文件
    for csv_file in csv_files:
        # 创建RPy文件名（如果已存在，则添加序号）
        base_name = os.path.basename(csv_file).replace('.csv', '')
        rpy_filename = os.path.join(text_dir, f"{base_name}.rpy")
        counter = 1

        while os.path.exists(rpy_filename):
            rpy_filename = os.path.join(text_dir, f"{base_name}_({counter}).rpy")
            counter += 1

        print(f"正在处理CSV文件: {csv_file}")
        print(f"将生成RPy文件: {rpy_filename}")

        # 读取CSV文件
        results = []
        with open(csv_file, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            headers = next(reader)  # 读取标题行
            for row in reader:
                if len(row) >= 5:  # 确保有足够的列
                    code_block = {
                        "定位符": row[0],
                        "特征码": row[1],
                        "原文注释": row[2],
                        "角色名": row[3],
                        "台词": row[4]
                    }
                    results.append(code_block)

        # 写入RPy文件
        with open(rpy_filename, 'w', encoding='utf-8') as f:
            for i, row in enumerate(results):
                # 写入定位符
                f.write(f"{row['定位符']}\n")

                # 写入特征码
                f.write(f"{row['特征码']}\n")

                # 写入空行
                f.write("\n")

                # 写入原文注释（4格缩进）
                f.write(f"    {row['原文注释']}\n")

                # 写入角色名和台词（4格缩进）
                if row['角色名']:  # 如果角色名不为空
                    f.write(f"    {row['角色名']} \"{row['台词']}\"\n")
                else:
                    f.write(f"    \"{row['台词']}\"\n")

                # 在每个代码块后添加一个空行，除了最后一个块
                if i < len(results) - 1:
                    f.write("\n")

        print(f"文件处理完成: {csv_file}")
        print(f"已生成RPy文件: {rpy_filename}")
        print(f"共处理 {len(results)} 条记录")
        print("-" * 50)


if __name__ == "__main__":
    csv_to_rpy()
