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
from episode import Episode
from s3_utils import s3_list_files, s3_delete_files, s3_copy_files

from season_service import download_all_seasons_episodes

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
# episode_service overview
#
# episodes = season_service.load_all_season_episodes()

# --------------------------
# load all episodes from the episode JSON files stored within 
# the S3 manifests directory, set 'episode_id' as 
# 'season_code' + 'episode_code'
 
# G = find_google_episode_keys_df(episode)
# --------------------------
# use google sheet data for episode_id to define 
# G with columns [episode_id, img_src, img_frame, randomized new_folder, manual new_img_class]

# C = s3_find_episode_jpg_keys_df(episode)
# --------------------------
# finds current jpg keys under tuttle_twins/ML with episode_id
# C with columns [last_modified, size, key, folder, img_class, img_frame, season_code, episode_code, episode_id]
# C -> columns [episode_id, img_frame, folder, img_class]

#-------------------------
# J1 = C join G on [episode_id, img_frame] to create
# J1 with columns [episode_id, img_frame, non-nullable img_class, non-nullable folder, nullable new_img_class, nullable new_folder] 
# J1 -> columns=[episode_id, img_frame, key, new_key]
# J1 discard rows where key == new_key
# J1 where new_key is null delete key file,
# J1 discard J1 rows with key in del_keys
# J1 where key != new_key copy key file to new_key file, delete key file,
# J1 discard J1 rows with key in del_keys

#-----------------------------
# J2 = G join C on [episode_id, img_frame] to create
# J2 = columns=[episode_id, img_frame, new_img_class, new_folder, nullable img_class, nullable folder ] 
# J2 assert when img_class is null then folder is null
# J2 -> columns [episode_id, img_frame, new_key, key]
# J2 assert new key is never null
# J2 assert size new_key == key is zero

#-----------------------------
# J3 = J2 join S on [episode_id, img_frame] to create
# J3 with columns = [episode_id, img_frame, new_key, key, img_src]
# J3 where new_key is not null and key is null copy img_src file to new_key file

#-----------------------------
# C4 =  fresh s3_find_episode_jpg_keys_df(episode)
# C4 with columns [last_modified, size, key, folder, img_class, img_frame, season_code, episode_code, episode_id]
# C4 -> columns [episode_id, img_frame, folder, img_class]

#-----------------------------
# G still has columns [episode_id, img_frame, randomized new_folder, manual new_img_class]
# G2 -> G with columns [episode_id, img_frame, folder, img_class]

#-----------------------------
# assert C4 == G2

def add_randomized_new_folder_column(df: pd.DataFrame) -> pd.DataFrame:
    '''
    per a percentage-wise distribution
    '''
    percentage_distribution = [
        ("train", 0.7),
        ("validate", 0.2),
        ("test", 0.1)
    ]
    choices = new_folders = [x[0] for x in percentage_distribution]
    weights = new_percentages = [x[1] for x in percentage_distribution]
    k = len(df)

    df['new_folder'] = random.choices(choices, weights=weights, k=k)
    return df


def find_google_episode_keys_df(episode: Episode) -> pd.DataFrame:
    '''
    Read all rows of a "google episode sheet" described in the given episode into 
    G with columns=[episode_id, img_src, img_frame, manual new_img_class]
    then add randomized new_folder column
    '''
    episode_id = episode.get_episode_id()

    # use the google credentials file and the episode's google_spreadsheet_share_link to read
    # the raw contents of the first sheet into G
    gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)
    gsheet = gc.open_by_url(episode.get_google_spreadsheet_share_link())
    data = gsheet.sheet1.get_all_records()
    df = pd.DataFrame(data)

    # fetch the public 's3_thumbnails_base_url' from the name of column zero, e.g.
    #   https://s3.us-west-2.amazonaws.com/media.angel-nft.com/tuttle_twins/s01e01/default_eng/v1/frames/thumbnails/
    s3_thumbnails_base_url = df.columns[0]

    # verify that s3_thumbnails_base_url contains episode_id.lower() e.g. s01e01
    episode_id_lower = episode_id.lower()
    if s3_thumbnails_base_url.find(episode_id_lower) == -1:
        raise Exception(f"s3_thumbnails_base_url fails to include {episode_id_lower}")

    # verify that all rows of the "FRAME NUMBER" column contain the 'episode_frame_code', e.g. 
    #   "TT_S01_E01_FRM"  
    # example FRAME_NUMBER column: 
    #   "TT_S01_E01_FRM-00-00-08-11"
    episode_frame_code = ("TT_" + episode.get_split_episode_id() + "_FRM").upper()
    matches = df[df['FRAME NUMBER'].str.contains(episode_frame_code, case=False)]
    failure_count = len(df) - len(matches)
    if failure_count > 0:
        raise Exception(f"{failure_count} rows have FRAME NUMBER values that don't contain 'episode_frame_code': {episode_frame_code}" )

    # save the episode_id in all rows
    df['episode_id'] = episode_id

    # store the "FRAME_NUMBER" column as "img_frame"
    df['img_frame'] = df["FRAME NUMBER"]

    # compute the "img_url" column of each row using the s3_thumbnails_base_url and the 'img_frame' of that row
    df['img_url'] = s3_thumbnails_base_url + df['img_frame'] + ".jpg"


    # compute the "new_class" column as the first available "CLASSIFICATION" for that row or None
    df['new_class'] = \
        np.where(df["JONNY's RECLASSIFICATION"].str.len() > 0, df["JONNY's RECLASSIFICATION"],
        np.where(df["SUPERVISED CLASSIFICATION"].str.len() > 0, df["SUPERVISED CLASSIFICATION"],
        np.where(df["UNSUPERVISED CLASSIFICATION"].str.len() > 0, df["UNSUPERVISED CLASSIFICATION"], None)))
    
    # add the randomized column 'new_folder'
    df = add_randomized_new_folder_column(df)
    
    # keep only these columns
    df = df[['episode_id', 'img_url','img_frame', 'new_folder', 'new_class']]

    return df

def split_key_in_df(df: pd.DataFrame) -> pd.DataFrame:
    '''
    split df.key to add columns ['folder', 'img_class', 'img_frame', 'season_code', 'episode_code', 'episode_id']
    '''
    # e.g. df.key = "tuttle_twins/ML/validate/Uncommon/TT_S01_E01_FRM-00-19-16-19.jpg"

    key_cols = ['tt','ml','folder','img_class','img_frame','ext']
    key_df = df['key'].str.split('/|\.', expand=True).rename(columns = lambda x: key_cols[x])
    key_df.drop(columns=['tt','ml','ext'], axis=1, inplace=True)
    # key_df = key_df[['folder','img_class','img_frame']]
    df = pd.concat([df,key_df], axis=1)

    # e.g. df.folder = "validate"
    # e.g. df.img_class = "Uncommon"
    # e.g. df.img_frame = "TT_S01_E01_FRM-00-19-16-19"
    
    img_frame_cols = ['tt', 'season_code', 'episode_code', 'remainder']
    img_frame_df = df.img_frame.str.split('_', expand=True).rename(columns = lambda x: img_frame_cols[x])
    img_frame_df.drop(columns=['tt','remainder'], axis=1, inplace=True)
    # img_frame_df = img_frame_df[['season_code','episode_code']]
    
    # e.g. df.season_code = "S01"
    # e.g. df.episode_code = "E01"
    
    img_frame_df['episode_id'] = img_frame_df.season_code + img_frame_df.episode_code
    # e.g. df.episode_id = "S01E01"

    df = pd.concat([df,img_frame_df], axis=1)   

    return df

def s3_find_episode_jpg_keys_df(episode: Episode) -> pd.DataFrame:
    '''
    find current jpg keys under tuttle_twins/ML with episode_id
    C = columns=[last_modified, size, key, folder, img_class, img_frame, season_code, episode_code, episode_id]
    C -> columns=[episode_id, img_frame, folder, img_class]
    '''
    bucket = S3_MEDIA_ANGEL_NFT_BUCKET
    dir = "tuttle_twins/ML"
    suffix = ".jpg"
    
    # example episode_id: S01E01, split_episode_id: S01_E01
    episode_id = episode.get_episode_id()

    # example key: tuttle_twins/ML/validate/Rare/TT_S01_E01_FRM-00-00-09-01.jpg
    episode_key_pattern = f"TT_{episode.get_split_episode_id()}_FRM-.+\.jpg"
    
    episode_keys_list = s3_list_files(bucket=bucket, dir=dir, key_pattern=episode_key_pattern)

    # create dataframe with columns ['last_modified', 'size', 'key']
    df = pd.DataFrame(episode_keys_list, columns=['last_modified', 'size', 'key'])
    
    # split df.key to add columns ['folder', 'img_class', 'img_frame', 'season_code', 'episode_code', 'episode_id']
    df = split_key_in_df(df)
    
    # assert df['episode_id'] == episode_id
    assert (df['episode_id'] == episode_id).all(), f"ERROR: expected 'episode_id' column to equal {episode_id}"

    df = df[['episode_id', 'img_frame', 'folder', 'img_class']]
    
    return df


def main() -> None:
    all_episodes = download_all_seasons_episodes()
    for episode in all_episodes:
        #-----------------------------
        G = find_google_episode_keys_df(episode)
        expected = set( G[['episode_id', 'img_src', 'img_frame', 'new_folder', 'new_img_class']] )
        result = set(G.columns)
        assert result == expected, f"ERROR: expected G.columns: {expected} not {result}"

        # --------------------------
        C = s3_find_episode_jpg_keys(episode)
        expected = set(C[['episode_id', 'img_frame', 'folder', 'img_class']])
        result = set(C.columns)
        assert result == expected, f"ERROR: expected C.columns: {expected} not {result}"

        #-------------------------
        # J1 = C join G on [episode_id, img_frame] to create
        # J1 with columns [episode_id, img_frame, non-nullable img_class, non-nullable folder, nullable new_img_class, nullable new_folder]         
        J1 = C.join(G,  
            how='inner', 
            on=['episode_id', 'img_frame'], 
            sort=False)
        expected = set(['episode_id', 'img_frame', 'img_class', 'folder', 'img_url', 'new_img_class', 'new_folder'])
        result = set(J1.columns)
        assert result == expected, f"ERROR: expected J1.columns: {expected} not {result}"
        
        # J1 -> columns=[episode_id, img_frame, key, new_key]
        J1['key'] = J1['folder'] + '/' + J1['img_class']
        J1['new_key'] = J1['new_folder'] + '/' + J1['new_img_class']
        J1 = J1[['episode_id', 'img_frame', 'key', 'new_key']]
        
        # J1 discard rows where key == new_key
        J1 = J1[J1['key'] != J1['new_key']]

        # J1 where new_key is null delete key file 
        J1_del = J1.dropna(subset=['new_key'])
        # tuttle_twins/ML/validate/Rare/TT_S01_E01_FRM-00-00-09-01.jpg 
        J1_del['del_key'] = "tuttle_twins/ML/" + J1_del['key'] + '/' + J1_del['img_frame'] + ".jpg"
        del_keys = list(J1_del['del_key'].to_numpy())
        s3_delete_files(bucket=S3_MEDIA_ANGEL_NFT_BUCKET, keys=del_keys)
        
        # discard J1 rows with key in del_keys
        # keep only J1 rows with key not in del_keys
        J1 = J1[J1['key'] not in del_keys]

        # J1 where key != new_key copy key file to new_key file, delete key file
        J1_cp = J1[(J1['key'] is not None) and (J1['new_key'] is not None) and (J1['key'] != J1['new_key'])]
        J1_cp['src_key'] = "tuttle_twins/ML/" + J1_cp['key'] + '/' + J1_del['img_frame'] + ".jpg"
        J1_cp['dst_key'] = "tuttle_twins/ML/" + J1_cp['new_key'] + '/' + J1_del['img_frame'] + ".jpg"
        J1_cp = J1_cp[['src_key','dst_key']]
        src_keys = list(J1_cp['src_key'].to_numpy())
        dst_keys = list(J1_cp['dst_key'].to_numpy())
        s3_copy_files(src_bucket=S3_MEDIA_ANGEL_NFT_BUCKET, src_keys=src_keys, 
                      dst_bucket=S3_MEDIA_ANGEL_NFT_BUCKET, dst_keys=dst_keys)
        del_keys = src_keys
        s3_delete_files(bucket=S3_MEDIA_ANGEL_NFT_BUCKET, keys=del_keys)
        
        # discard J1 rows with key in del_keys
        # keep only J1 rows with key not in del_keys
        J1 = J1[J1['key'] not in del_keys]

        #-----------------------------
        # J2 = G join C on [episode_id, img_frame] to create
        # J2 = columns=[episode_id, img_frame, new_img_class, new_folder, nullable img_class, nullable folder ] 
        # J2 assert when img_class is null then folder is null
        # J2 -> columns [episode_id, img_frame, new_key, key]
                
        J2 = G.join(C,  
            how='inner', 
            on=['episode_id', 'img_frame'], 
            sort=False)
        expected = set(['episode_id', 'img_frame', 'new_img_class', 'new_folder','img_class', 'folder'])
        result = set(J2.columns)
        assert result == expected, f"ERROR: expected J2.columns: {expected} not {result}"
        
        # J2 assert when img_class is null then folder is null
        # find J2a rows where img_class is null and folder is not null
        # find J2b rows where img_class is not null and folder is null
        J2a = J2[J2['img_class'] is None and J2['folder'] is not None]
        assert(len(J2a) == 0)
        J2b = J2[J2['img_class'] is not None and J2['folder'] is None]
        assert(len(J2b) == 0)
        
        # J2 -> columns [episode_id, img_frame, new_key, key]
        J2['new_key'] = J2['new_folder'] + '/' + J2['new_img_class']
        J2['key'] = J2['folder'] + '/' + J2['img_class']
        J2 = J2[['episode_id', 'img_frame', 'new_key', 'key']]

        # J2 assert size new_key == key is zero
        # J2 keep only rows where new_key != key and verify same length
        len1 = len(J2)
        J2 = J2[J2['new_key'] != J2['key']]
        assert len(J2) == len1

        #-----------------------------
        # J3 = J2 join S on [episode_id, img_frame] to create
        # J3 with columns = [episode_id, img_frame, new_key, key, img_src]
        S = G[['episode_id', 'img_frame', 'img_src']]
        J3 = J2.join(S,  
            how='inner', 
            on=['episode_id', 'img_frame'], 
            sort=False)
        expected = set(['episode_id', 'img_frame', 'new_keys', 'keyr','img_src'])
        result = set(J3.columns)
        assert result == expected, f"ERROR: expected J3.columns: {expected} not {result}"

        # J3 where new_key is not null and key is null copy img_src file to new_key file
        J3_cp = J3[J3['new_key'] is not None and J3['key'] is None]
        src_keys = J3_cp[J3_cp['img_src']]
        dst_keys = J3_cp[J3_cp['new_key']]
        s3_copy_files(src_bucket=S3_MEDIA_ANGEL_NFT_BUCKET, src_keys=src_keys, 
                      dst_bucket=S3_MEDIA_ANGEL_NFT_BUCKET, dst_keys=dst_keys)

        #-----------------------------
        # C4 = fresh s3_find_episode_jpg_keys(episode)
        # C4 with columns [last_modified, size, key, folder, img_class, img_frame, season_code, episode_code, episode_id]
        C4 = s3_find_episode_jpg_keys(episode)
        expected = set(['last_modified', 'size', 'key', 'folder', 'img_class', 'img_frame', 'season_code', 'episode_code', 'episode_id'])
        result = set(C4.columns)
        assert result == expected, f"ERROR: expected C4.columns: {expected} not {result}"
        
        # C4 -> columns [episode_id, img_frame, folder, img_class]
        C4 = C4[['episode_id', 'img_frame', 'folder', 'img_class']]

        #-----------------------------
        # G still has columns [episode_id, img_frame, new_folder, new_img_class]
        # G2 -> G with columns [episode_id, img_frame, folder, img_class]
        G2 = G
        G2 = G2.rename({'new_folder':'folder', 'new_img_class':'img_class'})
        expected = set(['episode_id', 'img_frame', 'folder', 'img_class'])
        result = set(G2.columns)
        assert result == expected, f"ERROR: expected G2.columns: {expected} not {result}"

        #-----------------------------
        # assert C4 == G2
        pd.testing.assert_frame_equal(C4,G2)


# =============================================
# TESTS
# =============================================

def test_s3_find_episode_jpg_keys_df():
    episode = Episode({"episode_id":"S01E01", "google_spreadsheet_title":"", "google_spreadsheet_url": "", "google_spreadsheet_share_link":""})
    C = s3_find_episode_jpg_keys_df(episode)
    expected = set(C[['episode_id', 'img_frame', 'folder', 'img_class']])
    result = set(C.columns)
    assert result == expected, f"ERROR: expected C.columns: {expected} not {result}"
    assert C is not None and len(C) > 0, "expected more than zero keys in C"


if __name__ == "__main__":
    main()

