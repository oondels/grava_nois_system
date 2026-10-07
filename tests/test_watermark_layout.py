import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from src.config.watermark_layout import validate_layout, image_rect
from src.services.watermark_catalog import prepare_assets, snapshot_layout


def fixture():
    asset = {'id': '11111111-1111-4111-8111-111111111111', 'sha256': 'a'*64, 'width': 200, 'height': 100}
    placement = lambda x,y: {'enabled': True, 'x': x, 'y': y, 'width': .2, 'height': .15, 'opacity': .6}
    return {'version': 2, 'clientEnabled': True, 'assets': dict.fromkeys(('institutional','clientBottom','clientTop'),asset),
        'default': {'institutional': placement(.4,.8), 'clientBottom': placement(.02,.8), 'clientTop': placement(.02,.02)}, 'cameras': {}}


class WatermarkLayoutTests(unittest.TestCase):
    def test_shared_geometry_fixtures(self):
        for case in json.loads((Path(__file__).parent/'fixtures/watermark-geometry.json').read_text()):
            self.assertEqual(list(image_rect(case['placement'], case['image']['width'], case['image']['height'], case['frame']['width'], case['frame']['height'])), case['rect'])

    def test_both_converters_preserve_layout_and_master_flag(self):
        import subprocess, os
        for repo in ('grava_nois_system','grava_nois_config'):
            script=Path(__file__).resolve().parents[2]/repo/'env_to_config.sh'
            if not script.exists(): continue
            with tempfile.TemporaryDirectory() as directory:
                env=Path(directory)/'.env';output=Path(directory)/'config.json'
                env.write_text('GN_WATERMARK_LAYOUT_JSON='+json.dumps(fixture(),separators=(',',':'))+'\nGN_CLIENT_WATERMARK_ENABLED=0\nGN_CAMERAS_JSON=[]\n')
                result=subprocess.run(['bash',str(script),str(env),str(output)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                result=json.loads(output.read_text())['processing']['watermark']['layout']
                self.assertFalse(result['clientEnabled']);self.assertEqual(result['assets'],fixture()['assets'])

    def test_overlap_bounds_and_institutional_validation(self):
        layout = fixture(); validate_layout(layout)
        for mutation in [lambda p: p['clientTop'].update(x=.4,y=.8), lambda p:p['institutional'].update(enabled=False),
                         lambda p:p['institutional'].update(opacity=0), lambda p:p['clientTop'].update(x=.99),
                         lambda p:p['clientTop'].update(width=float('nan'))]:
            broken=copy.deepcopy(layout); mutation(broken['default'])
            with self.assertRaises(ValueError): validate_layout(broken)
        layout['clientEnabled']=False;layout['default']['clientTop'].update(x=.4,y=.8)
        with self.assertRaises(ValueError):validate_layout(layout)

    def test_geometry_at_multiple_resolutions_and_aspects(self):
        p=fixture()['default']['institutional']
        for w,h in [(1280,720),(1920,1080),(640,480),(720,1280)]:
            x,y,iw,ih=image_rect(p,200,100,w,h)
            self.assertLessEqual(abs(iw/ih-2),.04)
            self.assertGreaterEqual(x,p['x']*w-1);self.assertGreaterEqual(y,p['y']*h-1)
            self.assertLessEqual(x+iw,(p['x']+p['width'])*w+1)
            self.assertLessEqual(y+ih,(p['y']+p['height'])*h+1)

    def test_download_integrity_and_snapshot_independence(self):
        import base64, struct
        content=b'\x89PNG\r\n\x1a\n'+b'\x00\x00\x00\rIHDR'+struct.pack('>II',200,100)
        layout=fixture();layout['assets']['institutional']['sha256']=hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            with patch('src.services.watermark_catalog.post',return_value={'content':base64.b64encode(content).decode()}): prepare_assets(layout,folder)
            saved,paths=snapshot_layout(layout,'camera',folder)
            layout['default']['institutional']['x']=.5
            self.assertEqual(saved['placements']['institutional']['x'],.4)
            self.assertTrue(Path(paths['institutional']).exists())
            Path(paths['institutional']).write_bytes(b'corrupt')
            with self.assertRaises(ValueError):snapshot_layout(layout,'camera',folder)

    def test_master_toggle_and_per_camera_override(self):
        layout=fixture();layout['clientEnabled']=False
        layout['cameras']['side']=copy.deepcopy(layout['default'])
        layout['cameras']['side']['institutional']['opacity']=.9
        with tempfile.TemporaryDirectory() as folder, patch('src.services.watermark_catalog.validate_bytes'):
            versions=Path(folder)/'client-versions';versions.mkdir();(versions/('a'*64+'.png')).write_bytes(b'x')
            saved,paths=snapshot_layout(layout,'side',folder)
            self.assertEqual(saved['placements']['institutional']['opacity'],.9)
            self.assertIsNone(paths['clientBottom'])
            self.assertIsNone(paths['clientTop'])

    def test_ffmpeg_real_pixels_match_geometry_and_opacity(self):
        import shutil, subprocess, struct, zlib
        if not shutil.which('ffmpeg'): self.skipTest('ffmpeg unavailable')
        from src.video.processor import add_image_watermark, ffprobe_metadata
        def chunk(kind,data):
            return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
        png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',200,100,8,6,0,0,0))+chunk(b'IDAT',zlib.compress((b'\x00'+b'\xff'*800)*100))+chunk(b'IEND',b'')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);logo=root/'logo.png';logo.write_bytes(png)
            for width,height,vertical in [(1280,720,False),(1920,1080,False),(640,480,False),(1280,720,True)]:
                with self.subTest(width=width,height=height,vertical=vertical):
                    source=root/'source.mp4';output=root/'out.mp4'
                    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',f'color=c=black:s={width}x{height}:r=1:d=1','-frames:v','1','-c:v','libx264','-crf','0','-threads','1',str(source)],check=True,capture_output=True)
                    placements=fixture()['default']
                    for opacity,p in zip((.6,.3,.9),placements.values()):p.update(imageWidth=200,imageHeight=100,opacity=opacity)
                    add_image_watermark(str(source),str(logo),str(output),secondary_watermark_path=str(logo),top_watermark_path=str(logo),crf=0,preset='ultrafast',threads=1,vertical_format=vertical,watermark_layout={'placements':placements})
                    meta=ffprobe_metadata(output);w,h=meta['width'],meta['height']
                    if vertical:self.assertEqual(w,int(height*9/16)//2*2)
                    rgb=subprocess.run(['ffmpeg','-v','error','-i',str(output),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-threads','1','-'],check=True,capture_output=True).stdout
                    for p in placements.values():
                        x,y,iw,ih=image_rect(p,200,100,w,h)
                        value=rgb[((y+ih//2)*w+x+iw//2)*3]
                        self.assertLessEqual(abs(value-round(255*p['opacity'])),4)
                        row=rgb[(y+ih//2)*w*3:(y+ih//2+1)*w*3]
                        visible=[i for i in range(max(0,x-3),min(w,x+iw+3)) if row[i*3]>40]
                        self.assertLessEqual(abs(min(visible)-x),1)
                        self.assertLessEqual(abs(max(visible)-(x+iw-1)),1)
