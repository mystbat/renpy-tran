import os
import shutil
import subprocess
import csv


def fix_csv_files():
    # 获取当前脚本的目录
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # 构建split_csv目录的路径
    csv_dir = os.path.join(script_dir, "split_csv")

    # 检查split_csv目录是否存在
    if not os.path.exists(csv_dir):
        print(f"错误: {csv_dir} 目录不存在!")
        return

    # 获取split_csv目录中所有不以-s.csv结尾的.csv文件
    csv_files = [f for f in os.listdir(csv_dir) if f.endswith('.csv') and not f.endswith('-s.csv')]

    if not csv_files:
        print(f"在{csv_dir}目录中没有找到需要处理的.csv文件!")
        return

    print(f"找到 {len(csv_files)} 个需要处理的CSV文件")

    # 对每个CSV文件进行处理
    for csv_file in csv_files:
        csv_path = os.path.join(csv_dir, csv_file)

        # 构建对应的-s.csv文件路径
        s_csv_file = os.path.splitext(csv_file)[0] + '-s.csv'
        s_csv_path = os.path.join(csv_dir, s_csv_file)

        # 检查对应的-s.csv文件是否存在
        if os.path.exists(s_csv_path):
            print(f"跳过已处理的文件: {csv_file}")
            continue

        print(f"\n处理文件: {csv_file}")

        # 1. 复制CSV内容到csv_message.txt
        message_path = os.path.join(script_dir, "csv_message.txt")
        try:
            # 读取CSV文件内容
            with open(csv_path, 'r', encoding='utf-8') as f:
                csv_content = f.read()

            # 写入内容到csv_message.txt
            with open(message_path, 'w', encoding='utf-8') as f:
                f.write(csv_content)

            print(f"已复制内容到 {message_path}")
        except Exception as e:
            print(f"复制内容时出错: {str(e)}")
            continue

        # 2. 确认复制完成，然后执行work_csv.py
        print("内容复制完成，准备执行work_csv.py...")

        # 3. 执行work_csv.py
        work_script = os.path.join(script_dir, "work_csv.py")
        if not os.path.exists(work_script):
            print(f"错误: work_csv.py文件不存在于 {script_dir} 目录")
            continue

        try:
            print("正在执行work_csv.py...")
            subprocess.run(["python", work_script], check=True)
            print("work_csv.py执行完成")
        except subprocess.CalledProcessError as e:
            print(f"执行work_csv.py时出错: {str(e)}")
            continue

        # 4. 将work_csv.py的输出保存为对应的-s.csv文件
        try:
            # 读取work_csv.py的输出（假设输出在text目录下的csv.txt）
            output_dir = os.path.join(script_dir, "text")
            if not os.path.exists(output_dir):
                print(f"错误: {output_dir}目录不存在，无法获取输出内容")
                continue

            output_path = os.path.join(output_dir, "csv.txt")
            if not os.path.exists(output_path):
                print(f"错误: {output_path}文件不存在，无法获取输出内容")
                continue

            # 读取输出内容
            with open(output_path, 'r', encoding='utf-8') as f:
                output_content = f.read()

            # 保存到split_csv目录，文件名与原CSV文件相同，但添加-s后缀
            target_path = os.path.join(csv_dir, s_csv_file)
            with open(target_path, 'w', encoding='utf-8') as f:
                f.write(output_content)

            print(f"已将处理后的CSV保存到 {target_path}")
        except Exception as e:
            print(f"保存处理后的CSV时出错: {str(e)}")

    print("\n所有文件处理完成!")


if __name__ == "__main__":
    fix_csv_files()
