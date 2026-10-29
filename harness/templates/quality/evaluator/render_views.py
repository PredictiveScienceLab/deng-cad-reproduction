"""Independent inspection images from submitted STEP files; no source execution."""
from pathlib import Path
import json
import math

def render(directory,destination):
    import cadquery as cq
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.colors import LightSource
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.gp import gp_Pln,gp_Pnt,gp_Dir
    directory=Path(directory);destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    results=[]
    for name in ('step-1','step-2','step-3','main-part','stand','assembly'):
        path=directory/(name+'.step')
        if not path.is_file() or path.is_symlink():
            results.append({'file':name+'.step','status':'unavailable','reason':'Missing/unsafe STEP'});continue
        try:
            if path.stat().st_size>100_000_000:raise ValueError('STEP exceeds size limit')
            shape=cq.importers.importStep(str(path)).val()
            if not shape.isValid() or not shape.Solids():raise ValueError('Invalid/non-solid shape')
            b=shape.BoundingBox();extent=max(b.xlen,b.ylen,b.zlen)
            if not math.isfinite(extent) or extent<=0:raise ValueError('Invalid extent')
            vertices,triangles=shape.tessellate(max(.03,extent/1000),.15)
            if len(triangles)>100000 or len(vertices)>200000:raise ValueError('Render tessellation limit exceeded')
            points=np.array([[v.x,v.y,v.z] for v in vertices]);polygons=[points[list(t)] for t in triangles]
            center=np.array([(b.xmin+b.xmax)/2,(b.ymin+b.ymax)/2,(b.zmin+b.zmax)/2]);half=extent*.55
            fig=plt.figure(figsize=(10,8),layout='constrained')
            for index,(label,elevation,azimuth) in enumerate([('Isometric',25,-55),('Along X',0,0),('Along Y',0,90),('Along Z',90,-90)],1):
                ax=fig.add_subplot(2,2,index,projection='3d')
                ax.add_collection3d(Poly3DCollection(polygons,facecolors='#a9cbd8',edgecolors='#a9cbd8',linewidths=0,shade=True,lightsource=LightSource(azdeg=315,altdeg=35)))
                ax.set(xlim=(center[0]-half,center[0]+half),ylim=(center[1]-half,center[1]+half),zlim=(center[2]-half,center[2]+half))
                ax.set_box_aspect((1,1,1));ax.set_proj_type('ortho');ax.view_init(elev=elevation,azim=azimuth)
                ax.set_title(label);ax.set_xlabel('X / mm');ax.set_ylabel('Y / mm');ax.set_zlabel('Z / mm')
                if label=='Along X':ax.set_xticks([]);ax.set_xlabel('')
                if label=='Along Y':ax.set_yticks([]);ax.set_ylabel('')
                if label=='Along Z':ax.set_zticks([]);ax.set_zlabel('')
            fig.suptitle(name+' — independent STEP views')
            target=destination/(name+'.png');fig.savefig(target,dpi=120);plt.close(fig)
            section_fig,axes=plt.subplots(1,3,figsize=(12,4),layout='constrained')
            section_records=[]
            for ax,(axis,u,v,label) in zip(axes,[(0,1,2,'X'),(1,0,2,'Y'),(2,0,1,'Z')]):
                direction=[0,0,0];direction[axis]=1
                operation=BRepAlgoAPI_Section(shape.wrapped,gp_Pln(gp_Pnt(*center),gp_Dir(*direction)),True)
                if not operation.IsDone():raise ValueError('Central section operation failed')
                section=cq.Shape.cast(operation.Shape());edge_count=0
                for edge in section.Edges():
                    samples,_=edge.sample(60);samples.append(edge.positionAt(1));coordinates=np.array([[p.x,p.y,p.z] for p in samples])
                    ax.plot(coordinates[:,u],coordinates[:,v],color='#17485e',linewidth=.8);edge_count+=1
                ax.set_aspect('equal');ax.grid(alpha=.2);ax.set_title(label+' = '+format(center[axis],'.3f')+' mm')
                ax.set_xlabel('XYZ'[u]+' / mm');ax.set_ylabel('XYZ'[v]+' / mm')
                section_records.append({'normal_axis':label,'offset_mm':float(center[axis]),'edge_count':edge_count})
            section_fig.suptitle(name+' — independent central sections')
            section_target=destination/(name+'-sections.png');section_fig.savefig(section_target,dpi=120);plt.close(section_fig)
            results.append({'file':name+'.step','status':'available','image':str(target),'section_image':str(section_target),'sections':section_records,'triangles':len(triangles),'note':'Views use submitted coordinates; central sections are screening views, not proof of every off-center hole or feature.'})
        except Exception as exc:
            plt.close('all');results.append({'file':name+'.step','status':'inconclusive','reason':f'{type(exc).__name__}: {exc}'})
    result={'views':results,'scope':'Independent orthographic/isometric views and three central sections per part. Targeted off-center sections and source-dependency evidence must be supplied with operator review when needed; images alone do not certify dimensions.'}
    (destination/'views.json').write_text(json.dumps(result,indent=2)+'\n')
    return result
