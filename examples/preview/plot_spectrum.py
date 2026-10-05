#!/usr/bin/env python3
"""Standalone rerun: python plot_spectrum.py [artifact_directory]. Requires Matplotlib."""
def render_plot(directory):
    """All rendering dependencies are inside this function for standalone export."""
    import csv
    import json
    import math
    import platform
    import textwrap
    from pathlib import Path
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    directory = Path(directory)
    style = json.loads((directory/'plot_style.json').read_text(encoding='utf-8'))
    metadata = json.loads((directory/'plot_metadata.json').read_text(encoding='utf-8'))
    with (directory/'plot_data.csv').open(encoding='utf-8',newline='') as handle:
        rows = list(csv.DictReader(handle))
    f = [float(row['frequency_Hz']) for row in rows]
    total = [float(row['RLtotal_dB']) if row['RLtotal_dB'] else None for row in rows]
    fundamental = [float(row['RL00_dB']) if row['RL00_dB'] else None for row in rows]
    finite = [value for series in [total,fundamental] for value in series if value is not None]
    specified_floor = style['zero_floor_dB']
    if specified_floor is not None and (not isinstance(specified_floor,(int,float)) or isinstance(specified_floor,bool)
                                        or not math.isfinite(specified_floor) or (finite and specified_floor>=min(finite))):
        raise ValueError('zero_floor_dB must be finite and strictly below all finite plotted dB values')
    floor = specified_floor if specified_floor is not None else min(finite,default=-10)-max(6,(max(finite,default=-10)-min(finite,default=-10))*.2)
    floor_policy = 'explicit_below_finite_minimum' if specified_floor is not None else 'auto_below_finite_minimum'
    lower = floor if any(row['zero_R']=='True' or row['zero_R00']=='True' for row in rows) else min(finite,default=-10)
    upper = max(finite+[-10,0])
    margin = max(3,(upper-lower)*.07)
    ylim = [lower-margin,upper+margin]
    xmargin = (f[-1]-f[0])*.02 if len(f)>1 else max(abs(f[0])*.02,1)
    xlim = [f[0]-xmargin,f[-1]+xmargin]
    segments = []; start = 0
    for i in range(1,len(f)):
        if f[i]-f[i-1] > metadata['max_gap_Hz']:
            segments.append((start,i)); start=i
    segments.append((start,len(f)))
    rc = {'font.family':style['font_family'], 'svg.fonttype':'none', 'pdf.fonttype':42,
          'axes.linewidth':.8, 'font.size':style['font_size'], 'savefig.dpi':style['dpi']}
    with plt.rc_context(rc):
        domain_text=[]
        for material,domains in metadata['material_domains'].items():
            for quantity,bounds in domains.items():
                label=f'{material}: measured {quantity} [{bounds[0]/1e9:g}, {bounds[1]/1e9:g}] GHz'
                domain_text.extend(textwrap.wrap(label,width=max(35,int(style['figsize_inches'][0]*12))))
        footer_lines=domain_text+['screening_only | file provenance and convergence not verified']
        width,height=style['figsize_inches']
        footer_height=.14*len(footer_lines)+.2
        fig=plt.figure(figsize=(width,height+footer_height),layout='constrained')
        grid=fig.add_gridspec(2,1,height_ratios=[height,footer_height])
        ax=fig.add_subplot(grid[0]); footer_ax=fig.add_subplot(grid[1]); footer_ax.set_axis_off()
        footer_label=footer_ax.text(0,1,'\n'.join(footer_lines),transform=footer_ax.transAxes,va='top',
                                   fontsize=7,linespacing=1.35,color='#555555')
        for index,(start,end) in enumerate(segments):
            for values,color,label,marker in [(total,style['total_color'],'RL total','o'),(fundamental,style['fundamental_color'],'RL 00','s')]:
                # NaN splits exact-zero values. Those values receive explicit floor markers.
                ax.plot([x/1e9 for x in f[start:end]], [x if x is not None else math.nan for x in values[start:end]],
                        color=color,linewidth=style['linewidth'],marker=marker,markersize=style['markersize'],
                        label=label if index==0 else None)
        zeros=[x/1e9 for x,row in zip(f,rows) if row['zero_R']=='True']
        if zeros:
            ax.scatter(zeros,[lower]*len(zeros),marker='v',color=style['total_color'],label='Exact R=0 (display floor)')
        zero_fundamental=[x/1e9 for x,row in zip(f,rows) if row['zero_R00']=='True']
        if zero_fundamental:
            ax.scatter(zero_fundamental,[lower]*len(zero_fundamental),marker='v',facecolors='none',edgecolors=style['fundamental_color'],label='Exact R00=0 (display floor)')
        threshold = metadata['threshold_R']
        if threshold>0:
            ax.axhline(10*math.log10(threshold),color='#555555',linestyle='--',linewidth=.8,label='Threshold')
        # Material-domain edges are tagged without changing the frequency samples.
        for material,domains in metadata['material_domains'].items():
            for quantity,bounds in domains.items():
                if len(bounds)==2:
                    for edge in bounds:
                        if f[0]<=edge<=f[-1]:
                            ax.axvline(edge/1e9,color='#aaaaaa',linestyle=':',linewidth=.6)
        ax.set(xlabel='Frequency (GHz)',ylabel='Reflection loss (dB)',ylim=ylim,
               xlim=(xlim[0]/1e9,xlim[1]/1e9),title=metadata['title'])
        ax.grid(True,alpha=.18); ax.legend(loc='best',fontsize=max(6,style['font_size']-2))
        fig.canvas.draw()
        footer_bounds=footer_label.get_window_extent(fig.canvas.get_renderer()).transformed(fig.transFigure.inverted())
        plot_bounds=ax.get_position()
        fig.savefig(directory/'reflection_loss.png',dpi=style['dpi'])
        fig.savefig(directory/'reflection_loss.svg',format='svg')
        if style.get('pdf',False): fig.savefig(directory/'reflection_loss.pdf',format='pdf')
        plt.close(fig)
    metadata.update({'ylim_dB':ylim,'xlim_Hz':xlim,'segment_count':len(segments),'dpi':style['dpi'],
                     'zero_display_floor_dB':floor,'zero_floor_policy':floor_policy,
                     'zero_floor_definition':'display-only position for exact zero; finite minimum minus max(6 dB, 20% of finite span) when automatic',
                     'domain_label_placement':'outside_plot_footer',
                     'domain_label_bbox_figure':[footer_bounds.x0,footer_bounds.y0,footer_bounds.x1,footer_bounds.y1],
                     'plot_bbox_figure':[plot_bounds.x0,plot_bounds.y0,plot_bounds.x1,plot_bounds.y1],
                     'versions':{'python':platform.python_version(),'matplotlib':matplotlib.__version__},
                     'gap_policy':'break between adjacent frequencies separated by more than max_gap_Hz',
                     'svg_editability':'text retained as text; curves are vector paths',
                     'zero_policy':'null RL for exact zero; triangle at labeled display floor; raw CSV unchanged'})
    (directory/'plot_metadata.json').write_text(json.dumps(metadata,indent=2,allow_nan=False),encoding='utf-8')

if __name__ == "__main__":
    import sys
    from pathlib import Path
    render_plot(Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parent)
