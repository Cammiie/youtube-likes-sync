import pytest
from test_media import audio_files
from ytlikes.media import run_ffmpeg, validate


@pytest.mark.parametrize('source,accepted',[('lossless.flac',True),('lossy.m4a',False)])
def test_encrypted_stream_copy_accepts_flac_and_refuses_aac(audio_files,tmp_path,source,accepted):
    # Synthetic fixtures only; this does not contact a streaming service.
    key='00112233445566778899aabbccddeeff'
    encrypted=tmp_path/'encrypted.mp4'
    result=run_ffmpeg(['-y','-v','error','-i',str(audio_files/source),'-map','0:a:0',
        '-c:a','copy','-strict','-2','-encryption_scheme','cenc-aes-ctr',
        '-encryption_key',key,'-encryption_kid','ffeeddccbbaa99887766554433221100',str(encrypted)])
    assert result.returncode==0
    output=tmp_path/'output.flac'
    result=run_ffmpeg(['-y','-v','error','-decryption_key',key,'-i',str(encrypted),
        '-c:a','copy',str(output)])
    if accepted:
        assert result.returncode==0
        validate(output,1)
    else:
        assert result.returncode!=0
