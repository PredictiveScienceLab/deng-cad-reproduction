"""Reload STEP exports and create software-rendered inspection views.
No GUI or graphics server is needed. Run: python output/inspect_model.py
"""
from pathlib import Path
import math, re, json
import xml.etree.ElementTree as ET
import numpy as np
from PIL import Image, ImageDraw
import cadquery as cq

OUT=Path(__file__).resolve().parent

def load(name):
    return cq.importers.importStep(str(OUT/name)).val()

def render(parts, path, direction=(1,-1,.8), size=(1200,1000), edges=True):
    d=np.asarray(direction,dtype=float); d/=np.linalg.norm(d)
    up=np.array([0.,0.,1.])
    if abs(np.dot(up,d))>.99: up=np.array([0.,1.,0.])
    right=np.cross(up,d); right/=np.linalg.norm(right)
    up=np.cross(d,right)
    basis=np.array([right,up,d]).T
    meshes=[]
    for s,color in parts:
        vv,tt=s.tessellate(.12,.10)
        v=np.array([a.toTuple() for a in vv]); t=np.array(tt)
        meshes.append((s,v,t,np.asarray(color)))
    allp=np.vstack([v@basis for s,v,t,c in meshes])
    lo=allp[:,:2].min(0); hi=allp[:,:2].max(0); center=(lo+hi)/2
    w,h=size; scale=min((w-100)/(hi[0]-lo[0]),(h-100)/(hi[1]-lo[1]))
    image=np.full((h,w,3),250,dtype=np.uint8)
    depth=np.full((h,w),-np.inf)
    light=d*.6+up*.8-right*.3; light/=np.linalg.norm(light)
    def project(v):
        p=v@basis
        p[:,:2]=(p[:,:2]-center)*scale
        p[:,0]+=w/2; p[:,1]=h/2-p[:,1]
        return p
    for s,v,t,color in meshes:
        p=project(v)
        for tri in t:
            pts=p[tri]; x0,y0,z0=pts[0]; x1,y1,z1=pts[1]; x2,y2,z2=pts[2]
            xmin=max(0,int(math.floor(min(x0,x1,x2)))); xmax=min(w-1,int(math.ceil(max(x0,x1,x2))))
            ymin=max(0,int(math.floor(min(y0,y1,y2)))); ymax=min(h-1,int(math.ceil(max(y0,y1,y2))))
            den=(y1-y2)*(x0-x2)+(x2-x1)*(y0-y2)
            if abs(den)<1e-10 or xmin>xmax or ymin>ymax: continue
            xx,yy=np.meshgrid(np.arange(xmin,xmax+1)+.5,np.arange(ymin,ymax+1)+.5)
            a=((y1-y2)*(xx-x2)+(x2-x1)*(yy-y2))/den
            b=((y2-y0)*(xx-x2)+(x0-x2)*(yy-y2))/den
            c=1-a-b
            zz=a*z0+b*z1+c*z2
            sub=depth[ymin:ymax+1,xmin:xmax+1]
            mask=(a>=-1e-7)&(b>=-1e-7)&(c>=-1e-7)&(zz>sub)
            if not mask.any(): continue
            norm=np.cross(v[tri[1]]-v[tri[0]],v[tri[2]]-v[tri[0]])
            norm/=max(np.linalg.norm(norm),1e-12)
            shade=.42+.58*max(0,np.dot(norm,light))
            rgb=np.clip(color*shade,0,255).astype(np.uint8)
            sub[mask]=zz[mask]; image[ymin:ymax+1,xmin:xmax+1][mask]=rgb
    if edges:
        for s,v,t,color in meshes:
            for edge in s.Edges():
                pts,_=edge.sample(.08)
                if len(pts)<2: continue
                pp=project(np.array([p.toTuple() for p in pts]))
                for p0,p1 in zip(pp[:-1],pp[1:]):
                    n=max(2,int(np.ceil(np.linalg.norm((p1-p0)[:2])*1.5)))
                    seg=p0+(p1-p0)*np.linspace(0,1,n)[:,None]
                    ix=np.floor(seg[:,0]).astype(int); iy=np.floor(seg[:,1]).astype(int)
                    valid=(ix>=0)&(ix<w)&(iy>=0)&(iy<h)
                    ix=ix[valid]; iy=iy[valid]; iz=seg[valid,2]
                    shown=iz>=depth[iy,ix]-.16
                    image[iy[shown],ix[shown]]=(45,50,57)
    Image.fromarray(image).save(str(OUT/path))

def line_render(shape,path,direction=(1,-1,.8),size=(1100,950)):
    opts=dict(width=size[0],height=size[1],marginLeft=35,marginTop=35,
              projectionDir=direction,showHidden=False,showAxes=False,strokeWidth=.4)
    svg=cq.exporters.getSVG(shape,opts)
    (OUT/Path(path).with_suffix('.svg')).write_text(svg)
    root=ET.fromstring(svg)
    transform=[float(v) for v in re.findall(r'[-+]?[0-9]*\.?[0-9]+(?:e[-+]?[0-9]+)?',root[0].get('transform'))]
    sx,sy,tx,ty=transform
    aa=2; im=Image.new('RGB',(size[0]*aa,size[1]*aa),'white'); draw=ImageDraw.Draw(im)
    for e in root.iter():
        if e.tag.endswith('path'):
            vals=[float(v) for v in re.findall(r'[-+]?[0-9]*\.?[0-9]+(?:e[-+]?[0-9]+)?',e.get('d',''))]
            points=[((x+tx)*sx*aa,(y+ty)*sy*aa) for x,y in zip(vals[::2],vals[1::2])]
            if len(points)>1: draw.line(points,fill=(20,25,30),width=2)
    im.resize(size,Image.Resampling.LANCZOS).save(str(OUT/path))

def main():
    grey=(202,211,222); blue=(100,151,194)
    one=load('step-1.step'); two=load('step-2.step'); main=load('main-part.step'); stand=load('stand.step')
    assembly=load('assembly.step')
    metrics={}
    for n in ['step-1.step','step-2.step','step-3.step','main-part.step','stand.step','assembly.step']:
        s=load(n); b=s.BoundingBox()
        metrics[n]=dict(valid=s.isValid(),solid_count=len(s.Solids()),volume_mm3=s.Volume(),
                        bounds_mm=[b.xmin,b.xmax,b.ymin,b.ymax,b.zmin,b.zmax])
    (OUT/'reload-measurements.json').write_text(json.dumps(metrics,indent=2)+'\n')
    print(metrics)
    render([(main,grey),(stand,blue)],'assembly-preview.png')
    render([(main,grey)],'main-preview.png')
    render([(stand,blue)],'stand-preview.png',direction=(1,-1,.9))
    render([(one,grey)],'stage1-preview.png')
    render([(two,grey)],'stage2-preview.png')
    # A longitudinal half section exposes the bore, tower passage and port.
    half=cq.Solid.makeBox(160,60,200,cq.Vector(-5,0,-90))
    main_half=main.intersect(half)
    render([(main_half,grey),(stand.intersect(half),blue)],'axial-section.png',direction=(0,-1,0))
    # A 0.02 mm slab through the common tower / lug plane is section B-B.
    slab=cq.Solid.makeBox(.02,110,200,cq.Vector(44.99,-55,-90))
    bb=main.intersect(slab)
    render([(bb,grey)],'transverse-section.png',direction=(-1,0,0))
    line_render(assembly,'assembly-lines.png')
    line_render(stand,'stand-lines.png')
    line_render(main_half,'axial-lines.png',direction=(0,-1,0))
    print('Rendered all exported geometry and sections.')

if __name__=='__main__': main()
