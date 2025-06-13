import os
import subprocess
import shutil


def main():
    while True:
        # 打印选项说明
        print("欢迎使用游戏本地化工具")
        print("请选择操作:")
        print("1. 分割文件 (运行 split.py)")
        print("2. 编辑配置文件 (config.ini)")
        print("3. AI批处理")
        print("4. 修复未完成内容")
        print("5. 合并输出")
        print("6. 退出")

        # 获取用户输入
        choice = input("请输入选项 (1-6): ").strip()

        if choice == "1":
            # 运行 split.py
            split_script = os.path.join(os.path.dirname(__file__), "split.py")
            if os.path.exists(split_script):
                subprocess.run(["python", split_script])
            else:
                print("错误: split.py 文件不存在!")

        elif choice == "2":
            # 编辑 config.ini
            config_path = os.path.join(os.path.dirname(__file__), "config.ini")
            if os.path.exists(config_path):
                # 根据操作系统打开编辑器
                if os.name == "nt":  # Windows
                    os.system(f'notepad "{config_path}"')
                elif os.name == "posix":  # Unix/Linux/Mac
                    if os.uname().sysname == "Darwin":  # Mac
                        os.system(f'open "{config_path}"')
                    else:  # Linux
                        os.system(f'xdg-open "{config_path}"')
                else:
                    print("未知操作系统，无法打开编辑器")
            else:
                print("错误: config.ini 文件不存在!")

        elif choice == "3":
            # AI批处理
            batch_process()

        elif choice == "4":
            # 修复未完成内容
            fix_missing_files()

        elif choice == "5":
            # 合并输出，调用同目录下的 merge.py
            merge_script = os.path.join(os.path.dirname(__file__), "merge.py")
            if os.path.exists(merge_script):
                subprocess.run(["python", merge_script])
            else:
                print("错误: merge.py 文件不存在!")

        elif choice == "6":
            print("感谢使用，再见!")
            break

        else:
            print("无效选项，请重新输入!")


def batch_process():
    # 创建必要的目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    split_dir = os.path.join(script_dir, "split_files")
    solved_dir = os.path.join(script_dir, "solved")  # 修正拼写错误

    if not os.path.exists(split_dir):
        print(f"错误: {split_dir} 目录不存在!")
        return

    if not os.path.exists(solved_dir):
        os.makedirs(solved_dir)  # 修正拼写错误

    # 获取所有rpy文件 - 修改为查找所有以数字序号.rpy结尾的文件
    rpy_files = []
    for filename in os.listdir(split_dir):
        if filename.endswith('.rpy'):
            # 检查文件名是否包含数字序号
            if any(char.isdigit() for char in filename):
                rpy_files.append(filename)

    if not rpy_files:
        print("错误: split_files目录中没有找到符合命名规则的rpy文件!")
        print("符合规则的文件名应该包含数字序号，例如: script_exp_split_001.rpy")
        return

    print(f"找到 {len(rpy_files)} 个符合规则的文件进行处理")

    # 按照数字序号排序
    rpy_files = sorted(rpy_files,
                       key=lambda f: int(''.join(c for c in f if c.isdigit())) if any(c.isdigit() for c in f) else 0)

    for i, rpy_file in enumerate(rpy_files):
        rpy_path = os.path.join(split_dir, rpy_file)

        # 读取并提取最前的文件文本内容
        try:
            with open(rpy_path, 'r', encoding='utf-8') as f:
                file_content = f.read()

            # 将内容写入message.txt，前面加上文件名注释
            message_path = os.path.join(script_dir, "message.txt")
            with open(message_path, 'w', encoding='utf-8') as f:
                # 添加文件名注释
                f.write(f"# {rpy_file}\n")
                # 添加内容
                f.write(file_content)

            print(f"已处理文件 {i + 1}/{len(rpy_files)}: {rpy_file}")

            # 运行work.py
            work_script = os.path.join(script_dir, "work.py")
            if os.path.exists(work_script):
                subprocess.run(["python", work_script])

                # 检查text目录是否存在
                text_dir = os.path.join(script_dir, "text")
                if not os.path.exists(text_dir):
                    os.makedirs(text_dir, exist_ok=True)

                # 检查content.txt是否存在
                content_path = os.path.join(text_dir, "content.txt")
                if os.path.exists(content_path):
                    # 读取content.txt的内容
                    with open(content_path, 'r', encoding='utf-8') as f:
                        ai_response = f.read()

                    # 创建原文件名的txt 文件
                    base_name = os.path.splitext(rpy_file)[0]  # 获取不带扩展名的文件名
                    txt_filename = f"{base_name}.txt"
                    txt_path = os.path.join(solved_dir, txt_filename)

                    # 将内容写入txt文件
                    with open(txt_path, 'w', encoding='utf-8') as f:
                        f.write(ai_response)

                    print(f"已将AI回答写入文件: {txt_filename}")
                else:
                    print(f"警告: {content_path} 文件不存在，跳过写入AI回答")
            else:
                print(f"警告: work.py 文件不存在，跳过处理")

            # 移动文件到solved目录
            solved_path = os.path.join(solved_dir, rpy_file)
            shutil.move(rpy_path, solved_path)
            print(f"文件已移动到solved目录: {rpy_file}")

        except FileNotFoundError:
            print(f"文件 {rpy_file} 不存在!")
        except Exception as e:
            print(f"处理文件 {rpy_file} 时出错: {str(e)}")

    print("所有文件处理完成!")


def fix_missing_files():
    # 获取当前脚本的目录
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # 构建fix.py的路径
    fix_script = os.path.join(script_dir, "fix.py")

    # 检查fix.py是否存在
    if not os.path.exists(fix_script):
        print(f"错误: {fix_script} 文件不存在!")
        return

    # 执行fix.py
    try:
        print("正在执行修复未完成内容...")
        subprocess.run(["python", fix_script])
        print("修复未完成内容完成!")
    except Exception as e:
        print(f"执行修复未完成内容时出错: {str(e)}")


if __name__ == "__main__":
    main()
