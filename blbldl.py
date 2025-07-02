from fake_useragent import UserAgent
from user_agents import parse
import re
import requests
import sys
from urllib.parse import unquote
from urllib.parse import urljoin
from lxml import etree
from bs4 import BeautifulSoup
import json
import time
import os
from pathlib import Path

def get_sec_ch_ua_mobile(ua):
    user_agent = parse(ua)
    if user_agent:
        return "?1"
    else:
        return "?0"

def get_sec_ch_ua(user_agent):
    match = re.search(r"Chrome/(\d+)", user_agent)
    if match:
        version = match.group(1)
        return f'"Chromium";v="{version}", "Google Chrome";v="{version}", "Not.A/Brand";v="99"'
    return ""
    
def get_platform(user_agent):
    if "iPhone" in user_agent or "iPad" in user_agent:
        return "iOS"
    elif "Mac OS X" in user_agent and "Mobile" not in user_agent:
        return "macOS"
    elif "Android" in user_agent:
        return "Android"
    elif "Windows" in user_agent:
        return "Windows"
    else:
        return "Unknown"

def extract_bv_id(link):
    try:
        decoded_url = unquote(link)
        pattern = r"(?:/video/|b23\.tv/)(BV[0-9A-Za-z]{10})"
        match = re.search(pattern, decoded_url)
        return match.group(1) if match else None
    except (AttributeError, TypeError):
        return None

def parse_bv_info(text):
    data = None
    data1 = None
    tree=etree.HTML(text)
    title="".join(tree.xpath('.//div[@class="video-info-title-inner"]/h1/text()'))
    title = re.sub(r'[\\/*?:"<>|]', '', title)
    title = title.strip(' .')
    soup = BeautifulSoup(text, 'html.parser')
    for script in soup.find_all('script'):
        script_text = script.string
        if script_text and 'window.__playinfo__' in script_text:
            match = re.search(
                r'window\.__playinfo__\s*=\s*({.*?})(?:\s*;|\s*$)',
                script_text,
                re.DOTALL
            )
            if match and len(match.group(1)) > 8000:
                json_str = match.group(1)
                if len(json_str) > 8000:
                    data = json.loads(json_str)
        if script_text and 'window.__INITIAL_STATE__' in script_text:
            match = re.search(
                r'window\.__INITIAL_STATE__\s*=\s*({.*?})(?:\s*;|\s*$)',
                script_text,
                re.DOTALL
            )
            if match and len(match.group(1)) > 8000:
                json_str = match.group(1)
                data1 = json.loads(json_str)

        if data and data1:  # Both JSONs found, no need to continue
            return data, data1
    return data, data1
def find_highest_quality_file_index(video_list):
    if not video_list:
        return -1
    highest_index = 0
    for index, video in enumerate(video_list):
        current_width = video.get('width', 0)
        current_height = video.get('height', 0)
        current_frame_rate = video.get('frameRate', video.get('frame_rate', 0))
        current_bandwidth = video.get('bandwidth', 0)
        highest_video = video_list[highest_index]
        highest_width = highest_video.get('width', 0)
        highest_height = highest_video.get('height', 0)
        highest_frame_rate = highest_video.get('frameRate', highest_video.get('frame_rate', 0))
        highest_bandwidth = highest_video.get('bandwidth', 0)
        if (current_width > highest_width or
                (current_width == highest_width and current_height > highest_height) or
                (
                        current_width == highest_width and current_height == highest_height and current_frame_rate > highest_frame_rate) or
                (
                        current_width == highest_width and current_height == highest_height and current_frame_rate == highest_frame_rate and current_bandwidth > highest_bandwidth)):
            highest_index = index
    return highest_index if video_list else -1
def get_media_info(bv_json):
    """从 BV JSON 中获取媒体（音频/视频）信息"""
    audio_list=[]
    try:
        # 尝试获取 FLAC 音频
        flac_data = bv_json.get("data", {}).get("dash", {}).get("flac", {}).get("audio", {})
        if flac_data is not None:
            if flac_data.get("baseUrl"):
                codecs = flac_data.get("codecs", "")
                return {
                    "link": flac_data["baseUrl"],
                    "audio_format": codecs.lower(),
                    "is_flac": True
                }
    except Exception as e:
        # 如果没有 FLAC，回退到普通音频
        audio_list = bv_json.get("data", {}).get("dash", {}).get("audio", [])
    if not audio_list:
        audio_list = bv_json.get("data", {}).get("dash", {}).get("audio", [])
    max_audio = audio_list[find_highest_quality_file_index(audio_list)]
    codecs = max_audio.get('codecs', "")
    return {
        "link": max_audio.get('baseUrl', ""),
        "audio_format": codecs.split('.')[0].lower(),
        "is_flac": False
    }

def main(link, output_dir):
    BVID = None
    if "www.bilibili.com" in link:
        BVID = link.split("/")[-1].split("?")[0]
    elif link.startswith("BV"):
        BVID = link

    if BVID:
        link = f"https://www.bilibili.com/video/{BVID}"
    bv_id = extract_bv_id(link)

    max_attempts = 10
    delay = 5
    media_info_json = None
    media_info_json1 = None

    for attempt in range(max_attempts):
        s = requests.Session()
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "accept-language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
            "cache-control": "max-age=0",
            "priority": "u=0, i",
            "referer": "https://www.bilibili.com/?spm_id_from=333.788.0.0",
            "sec-ch-ua": "\"Chromium\";v=\"136\", \"Google Chrome\";v=\"136\", \"Not.A/Brand\";v=\"99\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",  # Default, will be overwritten if needed
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            }
        # Update headers with potentially more accurate UA and platform info
        ua = UserAgent().random
        headers.update({
            "user-agent": ua,
            "sec-ch-ua": get_sec_ch_ua(ua),
            "sec-ch-ua-platform": get_platform(ua),
            "sec-ch-ua-mobile": get_sec_ch_ua_mobile(ua),
        })
        s.headers = headers
        r = s.get(url=link)
        media_info_json, media_info_json1 = parse_bv_info(r.text)

        if media_info_json:
            break
        else:
            print(f"Attempt {attempt + 1} failed. Retrying in {delay} seconds...")
            time.sleep(delay)

    if media_info_json:
        audio_info = get_media_info(media_info_json)
        audio_link = audio_info.get("link")
        output_filename = output_dir / "audio.mp3"
        
        max_download_attempts = 5
        for attempt in range(max_download_attempts):
            try:
                downloaded_size = 0
                if os.path.exists(output_filename):
                    downloaded_size = os.path.getsize(output_filename)

                resume_headers = s.headers.copy()
                if downloaded_size > 0:
                    resume_headers['Range'] = f'bytes={downloaded_size}-'
                
                print(f"开始下载音频... (尝试 {attempt + 1}/{max_download_attempts})")
                if downloaded_size > 0:
                    print(f"从 {downloaded_size} 字节处继续下载。")

                r = s.get(audio_link, stream=True, headers=resume_headers, timeout=60)
                
                file_mode = 'ab'
                if downloaded_size > 0 and r.status_code != 206:
                    print("警告: 服务器不支持断点续传，将重新开始下载。")
                    file_mode = 'wb' 
                
                r.raise_for_status()

                with open(output_filename, file_mode) as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                print("音频下载成功。")
                break
            except (requests.exceptions.RequestException, ConnectionError) as e:
                print(f"下载尝试 {attempt + 1} 失败: {e}")
                if attempt < max_download_attempts - 1:
                    sleep_time = 5 * (attempt + 1)
                    print(f"将在 {sleep_time} 秒后重试...")
                    time.sleep(sleep_time)
                else:
                    print("多次尝试后下载音频失败。")
                    return # 如果下载最终失败，则退出函数

        audio_json = {
                    "title":media_info_json1.get('videoData').get('title'),
                    "owner":media_info_json1.get('videoData').get('owner').get('name'),
                    "datetime":media_info_json1.get('videoData').get('ctime'),
                    "bvid":BVID}
        with open(output_filename.with_suffix('.json'), 'w', encoding='utf-8') as f:
            json.dump(audio_json, f, ensure_ascii=False)
    else:
        print("Failed to retrieve media information after multiple attempts.")

if __name__ == "__main__":
    link = sys.argv[1]
    main(link, Path())