
# from celery import shared_task
import logging
from jobs_project.celery import app
log = logging.getLogger("django")
from googlesearch import search
from .models import *
from bs4 import BeautifulSoup
import requests



def google(query,group_id):
    group = Group.objects.get(id=group_id)
    num_results = group.num_results
    search_results = search(query, num_results)
    print(search_results)
    return search_results

def get_links(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    links = [a['href'] for a in soup.find_all('a', href=True)]
    return links

def get_html_content(url):
    try:
        headers = {'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/42.0.2311.135 Safari/537.36 Edge/12.246"} 
        response = requests.get(url,headers=headers)
        if response.status_code == 200:
            return response.text
        else:
            print(f"Failed to retrieve HTML content. Status code: {response.status_code}")
            return None
    except Exception as e:
        print(f"Failed to retrieve HTML content: {e}")
        return None
    
def calculate_threshold(data,group_id):
    required_data = RequiredData.objects.filter(group=group_id)
    soup = BeautifulSoup(data, 'html.parser')
    text_content = soup.get_text().lower()

    total_score = 0
    total_found = 0
    for x in required_data:
        if x.word.lower() in text_content.lower():
            total_score += x.score
            total_found += 1
    if total_found == 0:
        return 0
    total_score = total_score/total_found
    return total_score

def scrape(url,group_id):
    html_content = get_html_content(url)
    if html_content == None:
        return []
    group = Group.objects.get(id=group_id)

    if html_content:
        links = get_links(html_content)
    else:
        links = []
    
    group = Group.objects.get(id=group_id)
    if calculate_threshold(html_content,group_id) >= group.threshold_value:
        Jobs.objects.create(url=url,group=group)
    print("GO have fun")
    return links


@app.task
def search_jobs(group_id):
    queries = Queries.objects.filter(group=group_id)
    final_list = []

    for x in queries:
        ls = google(x.query,group_id)
        for y in ls:
            if y not in final_list:
                final_list.append(y)
    
    for x in final_list:
        new_list = scrape(x,group_id)
        for y in new_list:
            if y not in final_list:
                final_list.append(y)

