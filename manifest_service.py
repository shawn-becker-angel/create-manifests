import pandas as pd
import numpy as np
import re
import gspread
import os
import time
import json
import random
from random import choices
import datetime
from manifest_row import ManifestRow
from episode import Episode
from manifest import Manifest
from s3_utils import find
import s3_utils

from season_service import download_all_season_episodes

import typing
from typing import Any, List, Dict

from constants import S3_MEDIA_ANGEL_NFT_BUCKET, S3_MANIFESTS_DIR, LOCAL_MANIFESTS_DIR

# use pip install python-dotenv
from dotenv import load_dotenv
load_dotenv()

# This JSON file is required for Google Drive API functions.
# This file is created manually by members of the Angel Studios 
# Data team.
# See the README.md file for instructions
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE")

# ============================================
# manifest MODULE OVERVIEW
#
# episodes = season_service.load_all_season_episodes()
# --------------------------
# load all episodes from the episode JSON files stored within 
# the S3 manifests directory, set 'episode_id' as 
# 'season_code' + 'episode_code'
# 
# manifest_df = create_new_manifest_from_google(episode)
# --------------------------
# use google sheet data to define a new 'manifest' dataframe 
# with columns 'episode_id', 'img_url', 'img_frame' and 'new_class'
#
# episode_keys_df = find_episode_keys(episode)
# --------------------------
# Use an S3 search to find all_keys currently in S3 with the given
# 'episode_id'.  Parse each 's3_key' to create dataset 'old_keys' 
# with columns 'episode_id', 'img_frame', 'old_class', 'old_folder',
# and 'old_key'
#
# manifest_df = init_manifest_old_keys(episode, manifest_df)
# --------------------------
# Use an inner join of 'manifest' against 'old_keys' on 'img_frame' 
# to initilize manifest columns 'old_folder', 'old_class', and 
# 'old_key'
#
# manifest_df = randomize_manifest_new_keys(manifest_df)
# --------------------------
# Randomly assign a 'new_folder' for each 'img_frame' in 'manifest'
# and compute 'new_key' from img_frame', 'new_class', and 'new_folder'
#
# manifest_df = sync_manifest(manifest_df, episode)
# --------------------------
# upodate manifest w/ switch:
#   Where 'new_key' matches 'old_key' do nothing
#
#   Where 'new_key' is not null copy 'src_url' to 'new_key' in s3
#
#   Where 'old_key' is not None and does not match 'new_key', delete 
#     'old_key' in s3 and set 'old_key' to None.
#
# re-run find_all_ml_keys(episode)
#   delete any 'old_key' in S3 that have no matching 'img_frame'.
#   Raise errors for any 'new_key' in S3 that have no matching 'img_frame'
#
# archive_manifest(manifest_df, episode)
# --------------------------
# Once all s3 changes are complete, set all 'old_key' to 'new_key',
# clear all 'new_key' and then use season_code, episode_code and 
# current utc_datetime_iso to save the versioned episode to the s3 
# manifests directory, for archival purposes.

def create_new_manifest_from_google(episode: Episode) -> Manifest:
    '''
    Read all rows of a "google episode sheet" described in the given
    "episode" into "manifest_df" with columns:
        ['episode_id', 'img_url','img_frame', 
        'old_class (None)', 'old_folder (None)', 'old_key (None)', 
        'new_class', 'new_folder (None)', 'new_key (None)']
    '''
    # get attributes from episode object
    season_code = episode["season_code"].upper()
    episode_code = episode["episode_code"].upper()
    google_spreadsheet_share_link = episode["google_spreadsheet_share_link"]
    google_spreadsheet_url = episode["google_spreadsheet_url"]

    # use the google credentials file and the episode's google_spreadsheet_share_link to read
    # the raw contents of the first sheet into manifest_df
    gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)
    gsheet = gc.open_by_url(google_spreadsheet_share_link)
    data = gsheet.sheet1.get_all_records()
    manifest_df = pd.DataFrame(data)

    num_rows = manifest_df.shape[0]
    # df.info(verbose=True)
    # df.describe(include='all')
    print(f"input spread_sheet_url:{google_spreadsheet_url} num_rows:{num_rows}")

    # fetch the public 's3_thumbnails_base_url' from the name of column zero, e.g.
    #   https://s3.us-west-2.amazonaws.com/media.angel-nft.com/tuttle_twins/s01e01/default_eng/v1/frames/thumbnails/
    s3_thumbnails_base_url = manifest_df.columns[0]

    # verify that s3_thumbnails_base_url contains 'episode_base_code', e.g. 
    #   "s01e01", which is constructed from episode properties "season_code" and "episode_code"
    episode_base_code = episode["season_code"].lower() + episode["episode_code"].lower()
    if s3_thumbnails_base_url.find(episode_base_code) == -1:
        raise Exception(f"s3_thumbnails_base_url fails to include 'episode_base_code': {episode_base_code}")

    # verify that all rows of the "FRAME NUMBER" column contain the 'episode_frame_code', e.g. 
    #   "TT_S01_E01_FRM"  
    # example FRAME_NUMBER column: 
    #   "TT_S01_E01_FRM-00-00-08-11"
    episode_frame_code = "TT_" + episode["season_code"].upper() + "_" + episode["episode_code"].upper() + "_FRM"
    matches = df[df['FRAME NUMBER'].str.contains(episode_frame_code, case=False)]
    failure_count = len(df) - len(matches)
    if failure_count > 0:
        raise Exception(f"{failure_count} rows have FRAME NUMBER values that don't contain 'episode_frame_code': {episode_frame_code}" )

    # save the episode_id
    manifest_df['episode_id'] = episode['episode_id']

    # compute the "img_url" column of each row using the s3_thumbnails_base_url and the "FRAME_NUMBER" of that row
    manifest_df['img_url'] = s3_thumbnails_base_url + manifest_df["FRAME NUMBER"] + ".jpg"

    # store the "FRAME_NUMBER" column as "img_frame"
    manifest_df['img_frame'] = manifest_df["FRAME NUMBER"]

    # these will be set from find_all_ml_keys
    manifest_df['old_class'] = None 
    manifest_df['old_folder'] = None

    # this depends upon "old_folder" and "old_class"
    manifest_df['old_key'] = None

    # compute the "new_class" column as the first available "CLASSIFICATION" for that row or None
    manifest_df['new_class'] = \
        np.where(manifest_df["JONNY's RECLASSIFICATION"].str.len() > 0, manifest_df["JONNY's RECLASSIFICATION"],
        np.where(manifest_df["SUPERVISED CLASSIFICATION"].str.len() > 0, manifest_df["SUPERVISED CLASSIFICATION"],
        np.where(manifest_df["UNSUPERVISED CLASSIFICATION"].str.len() > 0, manifest_df["UNSUPERVISED CLASSIFICATION"], None)))

    # this will be randomly assigned
    manifest_df['new_folder'] = None

    # this depends upon "new_folder" and "new_class"
    manifest_df['new_key'] = None
    
    # drop all columns except these used by ManifestRow dict
    manifest_df = manifest_df[
        ['episode_id', 'img_url','img_frame',
        'old_class','old_folder', 'old_key', 
        'new_class', 'new_folder', 'new_key']]

    return manifest_df


def find_episode_keys(episode: Episode) -> Manifest:
    '''
    Find all jpg file keys found under the ML folder in S3 that 
    contain the split_episode_key. Return a dataframe with columns:
    ['last_modified', 'size', 'key', 'folder', 'img_class', 'img_frame']
    '''
    bucket = S3_MEDIA_ANGEL_NFT_BUCKET
    dir = "tuttle_twins/ML"
    suffix = ".jpg"
    
    # example split_episode_id: S01_E01
    split_episode_id = Episode.split_episode_id(episode['episode_id'])

    # example key: tuttle_twins/ML/validate/Rare/TT_S01_E01_FRM-00-00-09-01.jpg
    episode_key_pattern = f"tuttle_twins/ML/\w+/w+/TT_{split_episode_id}_FRM-.+\.jpg"
    
    episode_keys_list = s3_utils.s3_list_files(bucket=bucket, dir=dir, key_pattern=episode_key_pattern)

    # create dataframe with columns ['last_modified', 'size', 'key']
    episode_keys_df = pd.DataFrame(episode_keys_list, columns=['last_modified', 'size', 'key'])
    
    # add columns ['folder', 'img_class', 'img_frame'] extracted from column key 
    episode_keys_df['folder'] = episode_keys_df['key'].str.extract(Episode.episode_key_folder_pattern,expand=True)
    episode_keys_df['img_class'] = episode_keys_df['key'].str.extract(Episode.episode_key_img_class_pattern,expand=True)
    episode_keys_df['img_frame'] = episode_keys_df['key'].str.extract(Episode.episode_key_img_frame_pattern,expand=True)

    return episode_keys_df

def init_manifest_old_keys(episode: Episode, manifest_df: Manifest) -> Manifest:

    # use an inner join of manifest_df with episode_keys_df to set the 
    # 'old_key', 'old_folder' and 'old_class' columns

    # manifest_df columns:
    # ['episode_id','img_url','img_frame',
    # 'old_key','old_class','old_folder',
    # 'new_key','new_class','new_folder]
    
    manifest_df = manifest_df[['episode_id','img_frame','new_class']]

    # episode_keys_df columns: 
    # ['last_modified', 'size', 'key', 'folder', 'img_class', 'img_frame']
    episode_keys_df = find_episode_keys(episode)
    episode_keys_df['old_class'] = episode_keys_df['img_class']
    episode_keys_df['old_folder'] = episode_keys_df['folder']
    # keep only these columns
    episode_keys_df = episode_keys_df[['img_frame', 'old_class', 'old_folder']]

    # expected columns of the inner join on img_frame
    expected_set  = set(['episode_id','img_url','img_frame',
    # 'old_key','old_class','old_folder',
    # 'new_key','new_class','new_folder]
        
        'img_url','img_frame','old_class','folder',, 'new_key','new__folder'])

    joined_df = manifest_df.join(
        episode_keys_df, 
        how='inner', 
        on=['img_frame','old_class'], 
        lsuffix='old_', 
        rsuffix='new_', 
        sort=False)

    result_set = set(joined_df.columns)
    assert join_columns_set == expected, f"unexpected join results: {result_set} != {expected_set}"

    manifest_df = joined_df

    # set 'ml_folder' to 'r_ml_folder'
    manifest_df['ml_folder'] = manifest_df['r_ml_folder']

    # keep only the ManifestRow columns
    manifest_df = manifest_df['img_url','img_frame','img_class','ml_folder', 'new_ml_folder']

    return manifest_df


def randomize_manifest_new_keys(episode: Episode, manifest_df: Manifest) -> Manifest:
    '''
    Set the new_folder column of teh given manifest_df 
    to a random new_folder per a percentage-wise distribution
    '''
    new_ml_folder_percentages = [
        ("train", 0.7),
        ("validate", 0.2),
        ("test", 0.1)
    ]
    choices = new_ml_folders = [x[0] for x in new_ml_folder_percentages]
    weights = new_ml_percentages = [x[1] for x in new_ml_folder_percentages]
    k = len(manifest_df)

    manifest_df['new_ml_folder'] = random.choices(choices, weights=weights, k=k)
    return manifest_df


def count_lines(filename) :
    with open(filename, 'r') as fp:
        num_lines = sum(1 for line in fp)
    return num_lines

# def get_all_ml_keys_df():

#     # set search criteria for all ML keys
#     bucket = S3_MEDIA_ANGEL_NFT_BUCKET
#     dir = "tuttle_twins/ML"
#     prefix = None
#     suffix = ".jpg"

#     all_ml_keys = s3_list_files(bucket=bucket, dir=dir, prefix=prefix, suffix=suffix)

#     # use list of s3_key dict to create dataframe with s3_key columns ["last_modified" , "size", "key"]
#     all_ml_keys_df = pd.DataFrame(all_ml_keys, columns=["last_modified" , "size", "key"])
#     return all_ml_keys_df

# def save_versioned_manifest_file(episode: Episode) -> str:
#     '''
#     Create a json lines file with the format of ManifestRow dict and save
#     it as an unversioned episode manifest file locally.

#     Arguments
#     ----------
#         episode: an Episode dict for an episode in a season

#     Returns
#     ----------
#         the name of the local unversioned manifest file
#     '''

#     # Use the given episode to create an manifest_df with undefined 
#     # 'new_folder' and 'new_ml_folder' columns from that episode's google shet
#     manifest_df = create_manifest_df_from_google_sheet(episode)

#     # Use init_ml_folder to initialize the 'ml_folder' from the current list of all ml_keys
#     init_old_ml_folder_column(manifest_df)

#     # compoute the path of the local unversioned manifest file
#     unversioned_manifest_file = f"{LOCAL_MANIFESTS_DIR}/{season_code}{episode_code}-manifest.jl"

#     # Write the df as json lines of format ManifestRow dict to the local JL file
#     manifest_df.to_json(unversioned_manifest_file, orient='records')
#     return unversioned_manifest_file


def randomize_new_ml_folder(manifest_df: pd.DataFrame) -> pd.DataFrame:
    '''
    Set the new_ml_folder column of teh given manifest_df 
    to a random new_folder per a percentage-wise distribution
    '''
    new_ml_folder_percentages = [
        ("train", 0.7),
        ("validate", 0.2),
        ("test", 0.1)
    ]
    choices = new_ml_folders = [x[0] for x in new_ml_folder_percentages]
    weights = new_ml_percentages = [x[1] for x in new_ml_folder_percentages]
    k = len(manifest_df)

    manifest_df['new_ml_folder'] = random.choices(choices, weights=weights, k=k)
    return manifest_df

# def compare_manifests_rows(
#     season_code: str, 
#     episode_code: str, 
#     local_rows: List[ManifestRow], 
#     remote_rows: List[ManifestRow]):
#     '''
#     given local and remote list of manifest rows
#     create a list of manifest action rows needed to bring
#     the remote rows in sync with the local rows

#     '''





# def df_from_manifest_rows(manifest: List[ManifestRow]) -> pd.DataFrame:
#     '''
#     ManifestRow: dict {src_url: str, dst_key: str}
#     '''
#     manifest_df = pd.DataFrame(manifest,columns=['src_url', 'dst_key'])
#     manifest_df.shape
#     return manifest_df


# def create_change_manifest_df(manifest_df):
#     # manifest_df 
#     # image_frame
#     # image_class
#     # folder clurrent location
#     # new_folder future randomized 
#     dst_folder_percentages = [
#         ("train", 0.7),
#         ("validate", 0.2),
#         ("test", 0.1)
#     ]
#     dst_folders = [x[0] for x in dst_folder_percentages]
#     dst_percentages = [x[1] for x in dst_folder_percentages]

#     df = manifest_df.copy(deep=True)
#     # {"src_url": "https://s3.us-west-2.amazonaws.com/media.angel-nft.com/tuttle_twins/s01e01/default_eng/v1/frames/stamps/TT_S01_E01_FRM-00-00-08-19.jpg", "dst_key": "tuttle_twins/ML/train/Common/TT_S01_E01_FRM-00-00-08-19.jpg"}

#     df['file'] = df['src_url].str.replace("")
#     change_manifest_df['dst_folder'] = random.choices(dst_folders, weights=dst_percentages, k=len(manifest_df))

#     change_manifest = []
#     for item in manifest:
#         change_manifest.append(file=item.file, old_folder=item.folder, new_folder=random_folder)
#     return change_manifest

# def create_copy_manifest(change_manifest):
#     copy_manifest = []
#     for change_item in change_manifest:
#         if change_item.old_remote_folder is null and change_item.new_remote_folder is not null:
#             copy_manifest.append(file=change_item.file, folder=ichange_item.new_remote_folder
#     return copy_manifest
             
# def create_delete_manifest(change_manifest):
#     delete_manifest = []
#     for change_item in change_manifest:
#         if change_item.old_remote_folder is not null and change_item.new_remote_folder is null:
#             delete_manifest.append(file=change_item.file, folder=change_item.old_remote_folder
#     return delete_manifest

# def apply_copy_manifest(copy_manifest):
#     for copy_item in copy_manifest:
#         copy(copy_item.file, copy_item.folder)

# def apply_delete_manifest(delete_manifest):
#     for delete_item in delete_manifest:
#         delete(delete_item.file, delete_item.folder)
             

#     manifest_df = shuffle_manifest_rows(manifest_df)


#     # write all rows of manifest_jl_file to a json lines file under local_manifests_dir
#     if not os.path.exists(LOCAL_MANIFESTS_DIR):
#         os.makedirs(LOCAL_MANIFESTS_DIR)

#     manifest_path = f"{LOCAL_MANIFESTS_DIR}/{manifest_jl_file}"
#     # write each row_dist to the manifest_jl_file as a flat row_json_str
#     with open(manifest_path, "w") as w: 
#         for row_dict in df_list_of_row_dicts:
#             row_json_str = json.dumps(row_dict) + "\n"
#             # row_json_str = row_json_str.replace("\\/","/")
#             w.write(row_json_str)
    
#     num_lines = count_lines(manifest_path)
#     print(f"output episode manifest_path:{manifest_path} num_lines:{num_lines}")


# def parse_manifest_version(manifest_key: str) -> str:
#     '''
#     Parse out the utc_timestamp_iso portion of the given manifest_key
#     as the manifest_version

#         Parameters
#         -----------
#             manifest_key (str)

#         Returns
#         ---------
#             manifest_version (str) or None if any exceptions were caught

#         Notes
#         -------
#             Example episode manifest key:
#             /tuttle_twins/manifests/S01E01-manifest-2022-05-02T12:43:24.662714.jl
            
#             Example episode manifest version - a utc_datetime_iso string:
#             2022-05-02T12:43:24.662714
#     '''
#     utc_datetime_iso_pattern = r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.\d{6})"
#     result = re.search(utc_datetime_iso_pattern, manifest_key)
#     version = result.group(1)
#     return version


# def download_all_s3_episode_objs() -> List[Dict]:
#     '''
#     Download the contents of all "season_manifest file"s 
#     found in s3. Each "season_manifest file" contains a list
#     of "episode dict"s. Return the concatonated list of all 
#     "episode dicts" found from all "season manifest file"s.

#         Parameters
#         ----------
#             None
        
#         Returns
#         -------
#             An unorderd list of all "Episode" dicts gathered from
#             all "season manifest file"s. 

#             An example list of all "Episode" dicts gathered from all "season_manifest file"s:
#                 [
#                     { # Episode dict for season 1 episode 1
#                         "season_code": "S01",
#                         "episode_code": "E01",
#                         ...
#                     },
#                     { # Episode dict for season 1 episode 2
#                         "season_code": "S01",
#                         "episode_code": "E02",
#                         ...
#                     },
#                     ...
#                     { # Episode dict for season 3 episode 11
#                         "season_code": "S03",
#                         "episode_code": "E11",
#                         ...
#                     },
#                     ... other Episode in other seasons
#                 ]
       
#         Notes
#         -------
#             The name of the "season_manifest file" that holds all Episode dicts for season 1:
#             S01-episodes.json

#             Example aws cli command to find all season manifest files:
#             aws s3 ls s3://media.angel-nft.com/tuttle_twins/manifests/ | egrep "S\d\d-episodes.json"

#     '''
#     bucket = S3_MEDIA_ANGEL_NFT_BUCKET
#     dir = "tuttle_twins/manifests"
#     prefix = "S"
#     suffix = "-episodes.json"

#     season_manifest_keys = s3_list_files(bucket=bucket, dir=dir, prefix=prefix, suffix=suffix, verbose=False)
#     if season_manifest_keys is None or len(season_manifest_keys) == 0:
#         logger.info("zero season manifest files found in s3 - returning empty list")
#         return []

#     all_episode_objs = []
#     for season_manifest_key in season_manifest_keys:
#         key = season_manifest_key['key']
#         try:
#             tmp_file = "/tmp/season-manifest-" + datetime.datetime.utcnow().isoformat()
#             s3_download_text_file(S3_MEDIA_ANGEL_NFT_BUCKET, key, tmp_file)
#             episode_objs = json.load(tmp_file) # List[Episode]
#             all_episode_objs.append(episode_objs)
#         finally:
#             os.remove(tmp_file)

#     return all_episode_objs

# def s3_download_manifest_file(bucket: str, key: str) -> List[ManifestRow]:
#     '''
#     download the contents of a single episode manifest file stored in s3
#         Parameters
#         ----------
#             bucket (str)
#             key (str)
        
#         Returns
#         -------
#             An unordered list of ManifestRow describing the contents of the given 
#             episode manifest file given its bucket and key 
            
#             Return an empty list if no manifest file is found.
        
#         Notes
#         -------
#             Example episode manifest key in s3:
#             /tuttle_twins/manifests/S01E01-manifest-2022-05-02T12:43:24.662714.jl
#         '''   
#     # create a temp file to hold the contents of the download
#     tmp_file = "/tmp/tmp-" + datetime.datetime.utcnow().isoformat()

#     manifest_rows = []
#     try:
#         # use bucket and key to download manifest json lines file to local tmp_file 
#         s3_download_text_file(manifest_bucket, manifest_key, tmp_file)

#         # The manifest file is a text file with a json string on each line.
#         # Decode each json_str into a json_dict and use ManifestRow to 
#         # verify its structure. Then append the manifest_row to list of all manifest_rows. 
#         with open(tmp_file, "r") as f:
#             for json_str in f:
#                 json_dict = json.loads(json_str)
#                 manifest_row = ManifestRow(json_dict)
#                 manifest_rows.append(manifest_row)
#     finally:
#         # delete the tmp_file whether exceptions were thrown or not
#         if os.path.exists(tmp_file):
#             os.remove(tmp_file)

#     return manifest_rows


# def download_latest_s3_manifest_file(season_code: str, episode_code: str) -> List[ManifestRow]:
#     '''
#     List the keys of all episode manifest files for the given season and 
#     episode in S3. Return an empty list if no keys were found.

#     Parse the 'version' string as the ending <utc_datetime_iso> 
#     portion of each key and add it as a new 'version' property of each key. 

#     Sort the list of keys by 'version' descending from highest to lowest.
#     The latest manifest_key is first in the sorted list.

#     Download and return the contents of latest manifest file as a list of 
#     ManifestRow dicts. 

#     Parameters
#     ----------
#         season_code (str): e.g. S01 for season 1
#         episode_code (str): e.g. E08 for episode 8
    
#     Returns
#     -------
#         An unordered list of ManifestRow describing the contents of the most recent episode manifest file 
#         for the given season and episode in S3. Return an empty list if no manifest file is found.
    
#     Notes
#     -------
#         Example s3 manifest filename:
#         S01E01-manifest-2022-05-03T22:53:30.325223.jl

#         Example aws cli command to find all episode manifest files for season 1 episode 1:
#         aws s3 ls s3://media.angel-nft.com/tuttle_twins/manifests/S01E01-manifest- | egrep ".*\.jl"

#         Example json string of an episode manifest file, with ManifestLine format:
#         {"src_url": "https://s3.us-west-2.amazonaws.com/media.angel-nft.com/tuttle_twins/s01e01/default_eng/v1/frames/stamps/TT_S01_E01_FRM-00-00-09-04.jpg", "dst_key": "tuttle_twins/ML/validate/Legendary/TT_S01_E01_FRM-00-00-09-04.jpg"}
#     '''
#     bucket = S3_MEDIA_ANGEL_NFT_BUCKET
#     dir = "tuttle_twins/manifests"
#     prefix = se_code =  (season_code + episode_code).upper()
#     suffix = ".jl"

#     manifest_keys = s3_list_files(bucket=bucket, dir=dir, prefix=prefix, suffix=suffix, verbose=False)
#     if manifest_keys is None or len(manifest_keys) == 0:
#         logger.info(f"zero episode manifest files found for {se_code} in s3 - returning empty list")
#         return []

#     # Parse the 'version' string as the ending <utc_datetime_iso> 
#     # portion of each key and add it as a new 'version' property of each key. 
#     for manifest_key in manifest_keys:
#         version = parse_manifest_version(manifest_key)
#         manifest_key['version'] = version
    
#     # sort the manifest_keys array by the last_modified attribute, descending from latest to earliest
#     # the zero element is the latest key
#     latest_manifest_key = sorted(manifest_keys, key=lambda x: x['version'], reverse=True)[0]

#     # download the contents of the manifest file as a list of ManifestRow dicts
#     manifest_rows = s3_download_manifest_file(S3_MEDIA_ANGEL_NFT_BUCKET, latest_manifest_key)
#     return manifest_rows


# def create_manifest_files():
#     '''
#     Each "season_manifest_file", e.g. "S01-episodes.json" in s3 
#     is a JSON file that contains a list of "episode_objs". Each
#     "episode dict" is used to create a versioned "manifest file" 

#     These season_manifest_files are JSON files that are managed 
#     manually by members of the Angel Studios Data team. 
#     '''

#     all_episode_objs = download_all_s3_episode_objs

#     # create an episode manifest f_dfile for each episode dict
#     for episode in all_episode_objs:
#         create_versioned_manifest_file(episode)

def process_all_seasons() -> None:
    all_episodes = download_all_season_episodes()
    for episode in all_episodes:
        manifest_df = create_new_manifest_from_google(episode)

        episode_keys_df = find_episode_keys(episode)

        manifest_df = init_manifest_old_keys(episode, manifest_df)

        manifest_df = randomize_manifest_new_keys(manifest_df)

        manifest_df = sync_manifest(manifest_df, episode)

        archive_manifest(manifest_df, episode)


# =============================================
# TESTS
# =============================================

def test_find_episode_keys():
    episode = Episode({"episode_id":"S01E01", "google_spreadsheet_title":"", "google_spreadsheet_url": "", "google_spreadsheet_share_link":""})
    episode_keys_df = find_episode_keys(episode)
    assert set(episode_keys_df.columns) == set(['last_modified', 'size', 'key'])
    assert episode_keys_df is not None and len(episode_keys_df) > 0, "expected more than zero episode_keys_df"

def test_parse_manifest_version():
    episode_management_key = "s3://media.angel-nft.com/tuttle_twins/manifests/S01E01-manifest-2022-05-02T12:43:24.662714.jl"
    expected = "2022-05-02T12:43:24.662714"
    result = parse_manifest_version(episode_management_key)
    assert result == expected, f"unexpected result:{result}"

if __name__ == "__main__":
    test_parse_manifest_version()

