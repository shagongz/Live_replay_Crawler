import re
import os
import time
import requests
import subprocess
import concurrent.futures
from playwright.sync_api import sync_playwright
from tqdm import tqdm


# ====================
# 工具函数
# ====================
def sanitize_filename(title):
    """清理标题中的非法字符"""
    return re.sub(r'[\\/*?:"<>|]', "", title).strip()


def process_input(text):
    """解析用户输入"""
    title_match = re.search(r'【(.*?)】', text)
    url_match = re.search(r'https?://\S+', text)
    return (title_match.group(1), url_match.group(0)) if title_match and url_match else (None, None)


# ====================
# 请求监控模块
# ====================
def monitor_requests(page):
    """监听并捕获音视频请求"""
    target_info = {
        "video_url": None,
        "audio_url": None,
        "cookies": None
    }

    def handle_request(request):
        url = request.url
        if "v3-web.douyinvod.com" in url:
            if "media-video-avc1" in url and not target_info["video_url"]:
                target_info["video_url"] = url
                target_info["cookies"] = page.context.cookies()
                print(f"捕获到视频请求：{url[:60]}...")

            if "media-audio-und-mp4a" in url and not target_info["audio_url"]:
                target_info["audio_url"] = url
                target_info["cookies"] = page.context.cookies()
                print(f"捕获到音频请求：{url[:60]}...")

    page.on("request", handle_request)
    return target_info


# ====================
# 下载模块
# ====================
class ChunkDownloader:
    def __init__(self, url, cookies, media_type):
        self.url = url
        self.cookies = {c["name"]: c["value"] for c in cookies}
        self.media_type = media_type
        self.chunk_size = 10 * 1024 * 1024  # 10MB分块
        self.max_workers = 4  # 并发线程数
        self.retries = 3  # 分块重试次数
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Referer": "https://www.douyin.com/"
        }

    def get_file_size(self):
        """获取文件总大小"""
        try:
            with requests.Session() as session:
                resp = session.head(
                    self.url,
                    cookies=self.cookies,
                    headers=self.headers,
                    allow_redirects=True
                )
                if resp.status_code == 200:
                    return int(resp.headers.get('content-length', 0))
                return 0
        except Exception as e:
            print(f"获取文件大小失败: {str(e)}")
            return 0

    def download_chunk(self, session, start, end, retry=0):
        """下载指定字节范围的分块"""
        headers = self.headers.copy()
        headers["Range"] = f"bytes={start}-{end}"

        try:
            with session.get(
                    self.url,
                    headers=headers,
                    cookies=self.cookies,
                    stream=True,
                    timeout=30
            ) as resp:
                if resp.status_code in (200, 206):
                    return start, resp.content
                raise Exception(f"状态码异常: {resp.status_code}")
        except Exception as e:
            if retry < self.retries:
                return self.download_chunk(session, start, end, retry + 1)
            raise Exception(f"分块下载失败: {str(e)} [bytes={start}-{end}]")

    def download(self, output_path):
        """执行分块下载"""
        total_size = self.get_file_size()
        if total_size == 0:
            raise Exception("无法获取有效文件大小")

        chunks = [(i, i + self.chunk_size - 1) for i in range(0, total_size, self.chunk_size)]
        chunks[-1] = (chunks[-1][0], total_size - 1)  # 修正最后一块

        temp_dir = f"temp_{os.path.basename(output_path)}"
        os.makedirs(temp_dir, exist_ok=True)

        print(f"开始下载{self.media_type} | 总大小: {total_size // 1024 // 1024}MB | 分块数: {len(chunks)}")

        with tqdm(total=total_size, unit='B', unit_scale=True, desc=self.media_type) as pbar:
            with requests.Session() as session:
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {}
                    for idx, (start, end) in enumerate(chunks):
                        future = executor.submit(
                            self.download_chunk, session, start, end
                        )
                        futures[future] = (idx, start, end)

                    for future in concurrent.futures.as_completed(futures):
                        idx, start, end = futures[future]
                        try:
                            chunk_start, data = future.result()
                            chunk_path = os.path.join(temp_dir, f"chunk_{idx:04d}.dat")
                            with open(chunk_path, "wb") as f:
                                f.write(data)
                            pbar.update(len(data))
                        except Exception as e:
                            print(f"\n分块下载失败: {str(e)}")
                            executor.shutdown(wait=False, cancel_futures=True)
                            raise

        # 合并分块文件
        with open(output_path, "wb") as final_file:
            for idx in range(len(chunks)):
                chunk_path = os.path.join(temp_dir, f"chunk_{idx:04d}.dat")
                with open(chunk_path, "rb") as chunk_file:
                    final_file.write(chunk_file.read())
                os.remove(chunk_path)
        os.rmdir(temp_dir)
        return True


# ====================
# 合并模块
# ====================
def merge_media(video_path, audio_path, output_path):
    """使用ffmpeg合并音视频"""
    try:
        cmd = [
            "ffmpeg",
            "-y",  # 覆盖输出文件
            "-i", video_path,
            "-i", audio_path,
            "-c", "copy",  # 直接流复制
            output_path
        ]
        subprocess.run(cmd, check=True, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError as e:
        print(f"合并失败：{e.stderr.decode()}")
        return False
    except FileNotFoundError:
        print("未找到ffmpeg，请先安装ffmpeg并添加到系统路径")
        return False


# ====================
# 主程序
# ====================
def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()

        while True:
            user_input = input("\n请输入内容（输入exit退出）:\n").strip()
            if user_input.lower() == "exit":
                break

            # 解析输入
            title, original_url = process_input(user_input)
            if not title or not original_url:
                print("输入格式错误，请检查后重试")
                continue

            # 处理文件名
            sanitized_title = sanitize_filename(title) or "untitled"
            output_filename = f"{sanitized_title}.mp4"

            # 创建新页面
            page = context.new_page()
            target_info = monitor_requests(page)

            try:
                # 访问目标页面
                print("正在加载页面...")
                page.goto(original_url, timeout=60000)

                # 等待捕获请求
                print("正在监听媒体请求...")
                start_time = time.time()
                while time.time() - start_time < 30:
                    if target_info["video_url"] and target_info["audio_url"]:
                        break
                    page.wait_for_timeout(1000)
                else:
                    print("错误：30秒内未捕获到完整的媒体请求")
                    continue

                # 下载视频
                print("\n开始下载视频流...")
                video_downloader = ChunkDownloader(
                    target_info["video_url"],
                    target_info["cookies"],
                    "视频"
                )
                video_downloader.download("temp_video.mp4")

                # 下载音频
                print("\n开始下载音频流...")
                audio_downloader = ChunkDownloader(
                    target_info["audio_url"],
                    target_info["cookies"],
                    "音频"
                )
                audio_downloader.download("temp_audio.mp4")

                # 合并媒体
                print("\n正在合并音视频...")
                if merge_media("temp_video.mp4", "temp_audio.mp4", output_filename):
                    print(f"\n✅ 合并完成：{output_filename}")

                # 清理临时文件
                for f in ["temp_video.mp4", "temp_audio.mp4"]:
                    try:
                        os.remove(f)
                    except:
                        pass

            except Exception as e:
                print(f"\n❌ 操作出错：{str(e)}")
            finally:
                page.close()

        browser.close()


if __name__ == "__main__":
    main()