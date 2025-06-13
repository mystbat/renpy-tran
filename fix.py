import os
import shutil
import subprocess


def fix_missing_files():
    # 获取当前脚本的目录
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # 构建solved目录的路径
    solved_dir = os.path.join(script_dir, "solved")

    # 检查solved目录是否存在
    if not os.path.exists(solved_dir):
        print(f"错误: {solved_dir} 目录不存在!")
        return

    # 获取solved目录中所有.rpy文件
    rpy_files = [f for f in os.listdir(solved_dir) if f.endswith('.rpy')]

    if not rpy_files:
        print(f"在{solved_dir}目录中没有找到.rpy文件!")
        return

    print(f"找到 {len(rpy_files)} 个.rpy文件需要处理")

    # 对每个.rpy文件进行处理
    for rpy_file in rpy_files:
        rpy_path = os.path.join(solved_dir, rpy_file)

        # 构建对应的.txt文件路径
        txt_file = os.path.splitext(rpy_file)[0] + '.txt'
        txt_path = os.path.join(solved_dir, txt_file)

        # 检查对应的.txt文件是否存在
        if os.path.exists(txt_path):
            print(f"跳过已处理的文件: {rpy_file}")
            continue

        print(f"\n处理文件: {rpy_file}")

        # 1. 复制rpy内容到message.txt
        message_path = os.path.join(script_dir, "message.txt")
        try:
            with open(rpy_path, 'r', encoding='utf-8') as f:
                rpy_content = f.read()

            with open(message_path, 'w', encoding='utf-8') as f:
                f.write(rpy_content)

            print(f"已复制内容到 {message_path}")
        except Exception as e:
            print(f"复制内容时出错: {str(e)}")
            continue

        # 2. 执行work.py
        work_script = os.path.join(script_dir, "work.py")
        if not os.path.exists(work_script):
            print(f"错误: work.py文件不存在于 {script_dir} 目录")
            continue

        try:
            print("正在执行work.py...")
            subprocess.run(["python", work_script], check=True)
            print("work.py执行完成")
        except subprocess.CalledProcessError as e:
            print(f"执行work.py时出错: {str(e)}")
            continue

        # 3. 复制text目录的content.txt内容
        text_dir = os.path.join(script_dir, "text")
        if not os.path.exists(text_dir):
            print(f"错误: {text_dir}目录不存在，无法复制content.txt")
            continue

        content_path = os.path.join(text_dir, "content.txt")
        if not os.path.exists(content_path):
            print(f"错误: {content_path}文件不存在，无法复制内容")
            continue

        try:
            # 读取content.txt内容
            with open(content_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # 保存到solved目录，文件名与rpy文件相同
            target_path = os.path.join(solved_dir, txt_file)
            with open(target_path, 'w', encoding='utf-8') as f:
                f.write(content)

            print(f"已将AI回答保存到 {target_path}")
        except Exception as e:
            print(f"保存AI回答时出错: {str(e)}")

    print("\n所有文件处理完成!")


if __name__ == "__main__":
    fix_missing_files()
