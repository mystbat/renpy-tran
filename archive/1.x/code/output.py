import os
import csv
import shutil


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    split_csv_dir = os.path.join(script_dir, "split_csv")
    csvbak_dir = os.path.join(script_dir, "csvbak")
    output_dir = os.path.join(script_dir, "output")

    # Step 1: 合并所有 -s.csv 文件
    if not os.path.exists(split_csv_dir):
        print(f"错误: {split_csv_dir} 目录不存在!")
        return

    # 获取所有 -s.csv 文件，并按序号排序
    s_files = [f for f in os.listdir(split_csv_dir) if f.endswith('-s.csv')]
    # 假设文件名格式为 "xxx_001-s.csv"，提取序号部分进行排序
    s_files.sort(key=lambda x: int(x.split('_')[-1].split('-s')[0]))

    merged_rows = []
    for file in s_files:
        file_path = os.path.join(split_csv_dir, file)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                rows = list(reader)
                # 跳过标题行，从第二列提取特征码和台词
                for row in rows[1:]:
                    if len(row) >= 3:
                        merged_rows.append(row)
        except Exception as e:
            print(f"读取文件 {file} 时出错: {str(e)}")
            continue

    # 写入合并后的文件
    lines_file = os.path.join(split_csv_dir, "script-lines.csv")
    try:
        with open(lines_file, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerows(merged_rows)
    except Exception as e:
        print(f"写入文件 {lines_file} 时出错: {str(e)}")
        return

    # Step 2: 复制 csvbak 中的同名文件到 output 目录
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    for file in os.listdir(csvbak_dir):
        if file.endswith('.csv'):
            src_path = os.path.join(csvbak_dir, file)
            dst_path = os.path.join(output_dir, file)
            try:
                shutil.copy(src_path, dst_path)
            except Exception as e:
                print(f"复制文件 {file} 时出错: {str(e)}")
                continue

    # Step 3: 更新 output 目录中的文件
    if not os.path.exists(lines_file):
        print(f"错误: 合并后的文件 {lines_file} 不存在!")
        return

    # 读取合并后的特征码到台词映射
    lines_features = {}
    try:
        with open(lines_file, 'r', encoding='utf-8') as f:
            lines_reader = csv.reader(f)
            for row in lines_reader:
                if len(row) >= 3:
                    lines_features[row[1]] = row[2]  # 特征码作为键，台词作为值
    except Exception as e:
        print(f"读取文件 {lines_file} 时出错: {str(e)}")
        return

    # 遍历 output 目录中的文件并更新台词列
    for file in os.listdir(output_dir):
        if file.endswith('.csv'):
            output_file_path = os.path.join(output_dir, file)
            try:
                with open(output_file_path, 'r', encoding='utf-8', newline='') as f:
                    output_reader = csv.reader(f)
                    output_rows = list(output_reader)

                # 创建新的行列表
                new_rows = []
                for row in output_rows:
                    if len(row) >= 5:
                        # 如果特征码存在，替换第五列（台词）
                        if row[1] in lines_features:
                            row[4] = lines_features[row[1]]
                    new_rows.append(row)

                # 写入更新后的文件
                with open(output_file_path, 'w', encoding='utf-8', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerows(new_rows)
            except Exception as e:
                print(f"处理文件 {file} 时出错: {str(e)}")
                continue

    print("所有操作已完成。")


if __name__ == "__main__":
    main()
