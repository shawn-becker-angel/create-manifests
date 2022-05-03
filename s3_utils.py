 # see https://stackoverflow.com/a/35805441/18218031

import argparse
import os
import sys
import pathlib
import filecmp
from typing import Optional

import boto3
s3_client = boto3.client('s3')

def s3_copy_file(src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> dict:
    '''
    returns the response dict
    '''
    response = s3_client.copy_object(
        CopySource={'Bucket': src_bucket, 'Key': src_key}, 
        Bucket=dst_bucket, 
        Key=dst_key
    )
    return response

def s3_list_files(bucket: str, dir: str, prefix: Optional[str], suffix: Optional[str], verbose: bool=False) -> int:
    '''
    returns the number of matching files
    '''
    prefix_str = "" if prefix is None else prefix + "*"
    suffix_str = "" if suffix is None else "*" + suffix

    if verbose:
        print(f"something like: aws s3 ls s3://{bucket}/{dir}/{prefix_str}{suffix_str}")

    if not dir.endswith("/"):
        dir += "/"

    response = s3_client.list_objects_v2(
        Bucket=bucket,
        Prefix=dir )

    num_found = 0
    for i, obj in enumerate(response["Contents"]):
        key = obj['Key']
        if prefix is None or prefix in key:
            if suffix is None or suffix in key:
                if verbose:
                    print(key, '\t', obj['LastModified'])
                num_found += 1
    
    if verbose:
        print(num_found, ("file" if num_found == 1 else "files"), "found")

    return num_found


def s3_upload_text_file(up_path, bucket, channel):
    '''
    upload a text file to s3
    '''
    up_file = os.path.basename(up_path)
    s3 = boto3.resource('s3')
    key = channel + "/" + up_file
    data = open(up_path, "rb")
    s3.Bucket(bucket).put_object(Key=key, Body=data)

def s3_download_text_file(bucket, key, dn_path):
    '''
    download a text file from s3
    '''
    s3 = boto3.resource('s3')
    s3.Bucket(bucket).download_file(key, dn_path)

def s3_list_file_cli():
    '''
    call s3_list_files() using command line arguments
    '''
    example = """
    python s3_utils media.angel-nft.com tuttle_twins/manifests --suffix .jl

    implements aws s3 ls s3://<bucket>/<dir>/[<prefix>]*[<suffix>]
    """
    parser = argparse.ArgumentParser(description='List files in S3.', usage=f"--help/-h <bucket> <dir> [--prefix <prefix>] [--suffix <suffix>]\nexample: {example}")
    parser.add_argument('bucket', 
                        help='an s3 bucket')
    parser.add_argument('dir', 
                        help='an s3 dir/ (or dir/dir/)')
    parser.add_argument('--prefix', metavar="<prefix>",
                        help='an optional filename prefix')
    parser.add_argument('--suffix', metavar="<suffix>",
                        help='an optional filename suffix')
    parser.add_argument("--verbose", "-v", help="increase output verbosity",
                        action="store_true")

    args = vars(parser.parse_args())

    bucket = args['bucket']
    dir =  args['dir']
    prefix = args['prefix']
    suffix = args['suffix']
    verbose = args['verbose']

    s3_list_files(bucket=bucket, dir=dir, prefix=prefix, suffix=suffix, verbose=verbose)


###################################################
# TESTS
###################################################

def test_s3_copy_file():
    '''
    {"src_url": "https://s3.us-west-2.amazonaws.com/media.angel-nft.com/tuttle_twins/s01e01/default_eng/v1/frames/stamps/TT_S01_E01_FRM-00-00-08-15.jpg", 
    "dst_key": "tuttle_twins/s01e01/ML/train/Common/TT_S01_E01_FRM-00-00-08-15.jpg"}
    '''
    src_bucket = "media.angel-nft.com"
    src_key = "tuttle_twins/s01e01/default_eng/v1/frames/stamps/TT_S01_E01_FRM-00-00-08-15.jpg"
    dst_bucket = "media.angel-nft.com"
    dst_key = "tuttle_twins/s01e01/ML/train/Common/TT_S01_E01_FRM-00-00-08-15.jpg"
    response = s3_copy_file(src_bucket, src_key, dst_bucket, dst_key)
    httpStatusCode = response['ResponseMetadata']['HTTPStatusCode']
    assert httpStatusCode == 200, f"bad httpStatusCode: {httpStatusCode}"


def test_s3_upload_download():
    tmp_dir = "/tmp"
    test_up_path = os.path.join(tmp_dir, "test_up.txt")
    test_dn_path = os.path.join(tmp_dir, "test_dn.txt")

    with open(test_up_path, "w") as f:
        f.write("HOWDY\n")

    bucket = "media.angel-nft.com"
    channel = "tuttle_twins/manifests"

    s3_upload_text_file(test_up_path, bucket, channel)

    up_file = os.path.basename(test_up_path)
    key = f"{channel}/{up_file}"
    s3_download_text_file(bucket, key, test_dn_path)

    # deep comparison
    result = filecmp.cmp(test_up_path, test_dn_path, shallow=False)
    assert result == True, "ERROR: files are not the same"

    # cleanup
    os.remove(test_up_path)
    os.remove(test_dn_path)

def test_s3_list_files():
    '''
    test s3_list_files using hard-coded values
    NOTE: this will fail if the following S3 URI is not found 
    s3://media.angel-nft.com/tuttle_twins/manifests/S01E01-manifest-2022-05-02T12:43:24.662714.jl
    '''
    bucket = "media.angel-nft.com"
    dir = "tuttle_twins/manifests"
    prefix = "S01E01-manifest"
    suffix = ".jl"

    result = s3_list_files(bucket=bucket, dir=dir, prefix=prefix, suffix=suffix, verbose=False)

    assert result > 0, "ERROR: s3_list_files returned zero manifest.jl files"


if __name__ == "__main__":

    # run tests if the only argv is this module name
    if len(sys.argv) == 1:
        test_s3_copy_file()
        test_s3_upload_download()
        test_s3_list_files()
    
    # run s3_list_file_cli if any command line args are given
    else:
        s3_list_file_cli()



