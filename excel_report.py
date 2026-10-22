"""AS workbook reports and native Excel charts, using only the standard library."""
from collections import defaultdict
from datetime import date
import io
import math
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
ET.register_namespace('', S)
ET.register_namespace('r', R)


def col(number):
    result = ''
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def text(value):
    return escape(''.join(ch for ch in str(value) if ch in '\t\n\r' or 32 <= ord(ch) <= 0xD7FF
                          or 0xE000 <= ord(ch) <= 0xFFFD or 0x10000 <= ord(ch) <= 0x10FFFF))


def analyse(rows, as_of):
    monthly = defaultdict(lambda: {'slips':0, 'sent':0, 'received':0, 'days':0, 'late':0})
    groups = defaultdict(lambda: {'slips':0, 'sent':0, 'received':0, 'days':0, 'late':0})
    outcomes = defaultdict(int)
    buckets = [0] * 5
    ledger = []
    totals = {'slips':len(rows), 'sent':0, 'received':0, 'pending':0, 'days':0, 'complete':0, 'partial':0}
    for slip in rows:
        start = date.fromisoformat(slip['date'])
        month = slip['date'][:7]
        requester = ' · '.join(slip.get('requesters', [])) or '미지정'
        labels = [('받는이', slip['supplier']), ('의뢰업체', requester)]
        monthly[month]['slips'] += 1
        for label in labels:
            groups[label]['slips'] += 1
        sent = sum(int(item['qty']) for item in slip['items'])
        totals['sent'] += sent
        monthly[month]['sent'] += sent
        for label in labels:
            groups[label]['sent'] += sent
        received_by_item = defaultdict(int)
        received_total = 0
        for event in slip.get('returns', []):
            days = max(0, (date.fromisoformat(event['date']) - start).days)
            entry_month = event['date'][:7]
            for entry in event['items']:
                qty = int(entry['qty'])
                item = slip['items'][entry['index']]
                received_by_item[entry['index']] += qty
                received_total += qty
                monthly[entry_month]['received'] += qty
                monthly[entry_month]['days'] += qty * days
                monthly[entry_month]['late'] += qty if days > 28 else 0
                totals['received'] += qty
                totals['days'] += qty * days
                outcomes[event.get('result') or '미지정'] += qty
                for label in labels:
                    groups[label]['received'] += qty
                    groups[label]['days'] += qty * days
                ledger.append([slip['number'], slip['date'], month, slip['supplier'], requester,
                               item.get('name',''), item.get('serial',''), entry.get('receivedSerial',''), qty,
                               event.get('result',''), event['date'], entry_month, days, event.get('mbRepairReport','')])
        age = max(0, (as_of - start).days)
        band = 0 if age <= 7 else 1 if age <= 14 else 2 if age <= 21 else 3 if age <= 28 else 4
        for index,item in enumerate(slip['items']):
            remaining = int(item['qty']) - received_by_item[index]
            buckets[band] += remaining
            totals['pending'] += remaining
            for label in labels:
                groups[label]['late'] += remaining if age > 28 else 0
        totals['complete'] += sent == received_total
        totals['partial'] += 0 < received_total < sent
    months = []
    first = min([as_of.strftime('%Y-%m'), *monthly])
    last = max([as_of.strftime('%Y-%m'), *monthly])
    year, month = map(int, first.split('-'))
    pending = 0
    while f'{year:04d}-{month:02d}' <= last:
        key = f'{year:04d}-{month:02d}'
        values = monthly[key]
        pending += values['sent'] - values['received']
        average = values['days'] / values['received'] if values['received'] else None
        months.append([key, values['slips'], values['sent'], values['received'], pending, average, values['late']])
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    suppliers = [[kind, name, value['slips'], value['sent'], value['received'], value['sent'] - value['received'],
                  value['days']/value['received'] if value['received'] else None, value['late']]
                 for (kind,name),value in sorted(groups.items(), key=lambda pair:(pair[0][0], -pair[1]['sent'], pair[0][1]))]
    return {'totals':totals, 'months':months, 'groups':suppliers, 'buckets':buckets,
            'outcomes':sorted(outcomes.items(), key=lambda pair:(-pair[1],pair[0])),
            'ledger':sorted(ledger, key=lambda entry:(entry[10],entry[0],entry[5]))}


STYLES = f'''<styleSheet xmlns="{S}">
<numFmts count="3"><numFmt numFmtId="164" formatCode="#,##0"/><numFmt numFmtId="165" formatCode="0.0"/><numFmt numFmtId="166" formatCode="0.0%"/></numFmts>
<fonts count="5"><font><sz val="11"/><name val="맑은 고딕"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="맑은 고딕"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="22"/><name val="맑은 고딕"/></font><font><color rgb="FF64748B"/><sz val="10"/><name val="맑은 고딕"/></font><font><b/><color rgb="FF16365D"/><sz val="24"/><name val="맑은 고딕"/></font></fonts>
<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF16365D"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFEDF3FA"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="2"><border/><border><bottom style="thin"><color rgb="FFD7E1EE"/></bottom></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="10">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="2" fillId="2" borderId="0" xfId="0"><alignment vertical="center"/></xf>
<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="3" fillId="3" borderId="0" xfId="0"><alignment vertical="center"/></xf>
<xf numFmtId="164" fontId="4" fillId="3" borderId="0" xfId="0"><alignment vertical="center"/></xf>
<xf numFmtId="165" fontId="4" fillId="3" borderId="0" xfId="0"><alignment vertical="center"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0"><alignment vertical="top"/></xf>
<xf numFmtId="165" fontId="0" fillId="0" borderId="1" xfId="0"><alignment vertical="top"/></xf>
<xf numFmtId="166" fontId="0" fillId="0" borderId="1" xfId="0"><alignment vertical="top"/></xf>
</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''


def cell(row, column, value, style):
    ref = f'{col(column)}{row}'
    if value is None or value == '':
        return f'<c r="{ref}" s="{style}"/>'
    if isinstance(value, (int,float)) and not isinstance(value, bool) and math.isfinite(value):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text(value)}</t></is></c>'


def sheet(table, widths, numeric=(), decimal=(), merges=(), overrides=None, dashboard=False):
    overrides = overrides or {}
    rows = []
    for r,values in enumerate(table,1):
        cells = []
        for c,value in enumerate(values,1):
            style = 0 if dashboard else 1 if r == 1 else 8 if c in decimal else 7 if c in numeric else 0
            cells.append(cell(r,c,value,overrides.get((r,c),style)))
        rows.append(f'<row r="{r}" ht="{18 if dashboard else 24 if r == 1 else 20}" customHeight="1">{"".join(cells)}</row>')
    columns = ''.join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>' for i,width in enumerate(widths,1))
    merge_xml = f'<mergeCells count="{len(merges)}">'+''.join(f'<mergeCell ref="{ref}"/>' for ref in merges)+'</mergeCells>' if merges else ''
    view = '<sheetView workbookViewId="0" tabSelected="1" showGridLines="0" zoomScale="80"/>' if dashboard else '<sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView>'
    filter_xml = '' if dashboard else f'<autoFilter ref="A1:{col(len(widths))}{len(table)}"/>'
    drawing = '<drawing r:id="rId1"/>' if dashboard else ''
    return (f'<worksheet xmlns="{S}" xmlns:r="{R}"><sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
            f'<sheetViews>{view}</sheetViews><cols>{columns}</cols><sheetData>{"".join(rows)}</sheetData>{filter_xml}{merge_xml}'
            '<pageMargins left="0.25" right="0.25" top="0.35" bottom="0.35" header="0.15" footer="0.15"/>'
            f'<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="{1 if dashboard else 0}"/>{drawing}</worksheet>')


def chart(title, kind, categories, series):
    def ref(formula, values, numeric):
        tag = 'num' if numeric else 'str'
        points = ''.join(f'<c:pt idx="{i}"><c:v>{text(v)}</c:v></c:pt>' for i,v in enumerate(values) if v is not None)
        return f'<c:{tag}Ref><c:f>{text(formula)}</c:f><c:{tag}Cache>' + ('<c:formatCode>General</c:formatCode>' if numeric else '') + f'<c:ptCount val="{len(values)}"/>{points}</c:{tag}Cache></c:{tag}Ref>'
    colors = ['2863DF','22A699','F5B638','F08B37','D94A48','8C6EC6','718096']
    series_xml = ''
    for i,(name,formula,values) in enumerate(series):
        fill = f'<c:spPr><a:solidFill><a:srgbClr val="{colors[i % len(colors)]}"/></a:solidFill></c:spPr>'
        if kind == 'line':
            fill = '<c:spPr><a:ln w="28575"><a:solidFill><a:srgbClr val="2863DF"/></a:solidFill></a:ln></c:spPr><c:marker><c:symbol val="circle"/><c:size val="5"/></c:marker>'
        if kind == 'doughnut':
            fill = ''.join(f'<c:dPt><c:idx val="{j}"/><c:spPr><a:solidFill><a:srgbClr val="{colors[j % len(colors)]}"/></a:solidFill></c:spPr></c:dPt>' for j in range(len(categories[1])))
        series_xml += f'<c:ser><c:idx val="{i}"/><c:order val="{i}"/><c:tx><c:v>{text(name)}</c:v></c:tx>{fill}<c:cat>{ref(*categories,False)}</c:cat><c:val>{ref(formula,values,True)}</c:val></c:ser>'
    axes = ''
    if kind == 'doughnut':
        plot = f'<c:doughnutChart><c:varyColors val="1"/>{series_xml}<c:dLbls><c:showLegendKey val="0"/><c:showVal val="1"/><c:showCatName val="0"/><c:showSerName val="0"/><c:showPercent val="0"/></c:dLbls><c:firstSliceAng val="270"/><c:holeSize val="60"/></c:doughnutChart>'
    else:
        if kind == 'bar':
            plot = f'<c:barChart><c:barDir val="col"/><c:grouping val="clustered"/><c:varyColors val="0"/>{series_xml}<c:gapWidth val="90"/><c:axId val="100"/><c:axId val="200"/></c:barChart>'
        else:
            plot = f'<c:lineChart><c:grouping val="standard"/><c:varyColors val="0"/>{series_xml}<c:axId val="100"/><c:axId val="200"/></c:lineChart>'
        axes = ('<c:catAx><c:axId val="100"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:delete val="0"/><c:axPos val="b"/><c:tickLblPos val="nextTo"/><c:crossAx val="200"/><c:crosses val="autoZero"/><c:auto val="1"/><c:lblAlgn val="ctr"/><c:lblOffset val="100"/></c:catAx>'
                '<c:valAx><c:axId val="200"/><c:scaling><c:orientation val="minMax"/><c:min val="0"/></c:scaling><c:delete val="0"/><c:axPos val="l"/><c:majorGridlines/>'
                f'<c:numFmt formatCode="{0.0 if kind == "line" else "#,##0"}" sourceLinked="0"/><c:tickLblPos val="nextTo"/><c:crossAx val="100"/><c:crosses val="autoZero"/><c:crossBetween val="between"/></c:valAx>')
    return (f'<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><c:lang val="ko-KR"/><c:chart>'
            f'<c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="ko-KR" sz="1300" b="1"/><a:t>{text(title)}</a:t></a:r></a:p></c:rich></c:tx><c:overlay val="0"/></c:title>'
            f'<c:autoTitleDeleted val="0"/><c:plotArea><c:layout/>{plot}{axes}</c:plotArea><c:legend><c:legendPos val="b"/><c:overlay val="0"/></c:legend><c:plotVisOnly val="1"/><c:dispBlanksAs val="gap"/></c:chart></c:chartSpace>')


def drawing():
    anchors = []
    for i,(left,top,right,bottom) in enumerate([(0,11,7,25),(7,11,14,25),(0,26,7,40),(7,26,14,40)],1):
        anchors.append(f'<xdr:twoCellAnchor><xdr:from><xdr:col>{left}</xdr:col><xdr:colOff>95250</xdr:colOff><xdr:row>{top}</xdr:row><xdr:rowOff>95250</xdr:rowOff></xdr:from><xdr:to><xdr:col>{right}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{bottom}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to><xdr:graphicFrame macro=""><xdr:nvGraphicFramePr><xdr:cNvPr id="{i}" name="AS Chart {i}"/><xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr><xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" r:id="rId{i}"/></a:graphicData></a:graphic></xdr:graphicFrame><xdr:clientData/></xdr:twoCellAnchor>')
    return '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="'+R+'">'+''.join(anchors)+'</xdr:wsDr>'


def add_as_report(original, records, as_of, version):
    report = analyse(records, as_of)
    totals, months = report['totals'], report['months']
    outcomes = report['outcomes'] or [('입고 내역 없음',0)]
    chart_outcomes = outcomes if len(outcomes) <= 8 else outcomes[:7] + [('그 외 유형',sum(qty for _,qty in outcomes[7:]))]
    dashboard = [[None]*14 for _ in range(max(51,46+len(outcomes))) ]
    overrides, merges = {}, ['A1:N2','A3:N3','A4:N4','A10:N10','A41:N41','A42:N42']
    dashboard[0][0] = 'AS 업무 종합 보고서'
    dashboard[2][0] = f'기준일 {as_of.isoformat()} | 전체 누적 내역 | V{version}'
    dashboard[3][0] = '발송: 접수일(출고일) 기준 / 입고: 실제 입고일 기준 / 모든 수량은 제품 개수'
    cards = [('A','C',0,'총 발송수량',totals['sent']),('D','F',3,'총 입고수량',totals['received']),
             ('G','I',6,'현재 미입고수량',totals['pending']),('J','N',9,'평균 입고 소요일',totals['days']/totals['received'] if totals['received'] else None)]
    for left,right,c,label,value in cards:
        merges.extend([f'{left}6:{right}6', f'{left}7:{right}8'])
        dashboard[5][c], dashboard[6][c] = label, value if value is not None else '입고 없음'
        end = ord(right) - ord('A') + 1
        for column in range(c+1,end+1):
            overrides[(6,column)] = 4
            for row in (7,8): overrides[(row,column)] = 6 if c==9 else 5
    rate = totals['received']/totals['sent']*100 if totals['sent'] else 0
    dashboard[9][0] = f"전체 {totals['slips']}전표 · 완료 {totals['complete']}전표 · 부분완료 {totals['partial']}전표 · 수량 기준 입고율 {rate:.1f}%"
    dashboard[40][0] = '평균 입고 소요일 = Σ(입고 수량 × 발송~입고 일수) ÷ 총 입고 수량. 미입고 제품은 평균에서 제외.'
    dashboard[41][0] = '월별 그래프는 최근 24개월. 전체 기간은 월별 입출고 시트, 개별 소요일은 입고 상세 시트에서 확인하세요.'
    dashboard[43][0], dashboard[43][4] = '미입고 경과일 구간 (수량)', '입고처리 유형 (입고 수량)'
    dashboard[43][8], dashboard[44][8:10] = '입고처리 그래프 원본', ['입고처리','수량']
    dashboard[44][0:2], dashboard[44][4:6] = ['경과일','미입고수량'], ['입고처리','수량']
    labels = ['0~7일','8~14일','15~21일','22~28일','29일 이상']
    for i,(label,qty) in enumerate(zip(labels,report['buckets']),45): dashboard[i][0:2] = [label,qty]
    for i,(label,qty) in enumerate(outcomes,45): dashboard[i][4:6] = [label,qty]
    for i,(label,qty) in enumerate(chart_outcomes,45): dashboard[i][8:10] = [label,qty]
    for r in (1,2):
        for c in range(1,15): overrides[(r,c)] = 2
    for r in (3,4,10,41,42):
        for c in range(1,15): overrides[(r,c)] = 3
    for c in (1,2,5,6,9,10): overrides[(45,c)] = 1
    tables = {
        3: sheet(dashboard,[12]*14,merges=merges,overrides=overrides,dashboard=True),
        4: sheet([['월','발송전표수','발송수량','입고수량','월말 미입고수량','평균 입고 소요일','28일 초과 입고수량'],*months],[16,16,16,16,21,23,24],numeric=(2,3,4,5,7),decimal=(6,)),
        5: sheet([['분류','업체','전표수','발송수량','입고수량','미입고수량','평균 입고 소요일','28일 초과 미입고수량'],*report['groups']],[16,30,14,16,16,18,24,26],numeric=(3,4,5,6,8),decimal=(7,)),
        6: sheet([['전표번호','접수일 (출고일)','출고월','받는이','의뢰업체','품명','출고 시리얼번호','입고 시리얼번호','입고수량','입고처리','입고일','입고월','발송~입고 경과일수','M/B 수리내역서 작성'],*report['ledger']],[22,20,15,25,18,32,28,32,14,22,20,15,24,45],numeric=(9,13))
    }
    recent = months[-24:]
    start, end = len(months)-len(recent)+2, len(months)+1
    categories = (f"'월별 입출고'!$A${start}:$A${end}", [row[0] for row in recent])
    charts = [chart('월별 발송 · 입고 수량 (최근 24개월)','bar',categories,
                    [('발송수량',f"'월별 입출고'!$C${start}:$C${end}",[row[2] for row in recent]),('입고수량',f"'월별 입출고'!$D${start}:$D${end}",[row[3] for row in recent])]),
              chart('월별 평균 입고 소요일 (일)','line',categories,[('평균 소요일',f"'월별 입출고'!$F${start}:$F${end}",[row[5] for row in recent])]),
              chart('현재 미입고 경과일 분포','doughnut',("'AS 보고서'!$A$46:$A$50",labels),[('미입고수량',"'AS 보고서'!$B$46:$B$50",report['buckets'])]),
              chart('입고처리 유형별 수량' + (' (상위 7개 + 그 외)' if len(outcomes)>8 else ''),'doughnut',(f"'AS 보고서'!$I$46:$I${45+len(chart_outcomes)}",[row[0] for row in chart_outcomes]),[('입고수량',f"'AS 보고서'!$J$46:$J${45+len(chart_outcomes)}",[row[1] for row in chart_outcomes])])]
    with zipfile.ZipFile(io.BytesIO(original)) as source:
        parts = {name:source.read(name) for name in source.namelist()}
    main = ET.fromstring(parts['xl/worksheets/sheet1.xml'])
    data_rows = main.findall(f'{{{S}}}sheetData/{{{S}}}row')
    headers = [c.find(f'{{{S}}}is/{{{S}}}t').text for c in data_rows[0]]
    numeric_columns = {col(i+1) for i,name in enumerate(headers) if name in ('수량','반환수량','미반환수량','경과일수')}
    for r,row in enumerate(data_rows):
        for c in row:
            if r == 0:
                c.set('s','1')
            elif re_column(c.attrib['r']) in numeric_columns:
                value = c.find(f'{{{S}}}is/{{{S}}}t').text or '0'
                for child in list(c): c.remove(child)
                c.attrib.pop('t',None); c.set('s','7')
                ET.SubElement(c,f'{{{S}}}v').text = str(int(value))
        if r == 0: row.set('ht','32'); row.set('customHeight','1')
    parts['xl/worksheets/sheet1.xml'] = ET.tostring(main,encoding='utf-8')
    workbook = ET.fromstring(parts['xl/workbook.xml'])
    views = ET.Element(f'{{{S}}}bookViews')
    ET.SubElement(views,f'{{{S}}}workbookView',activeTab='2',firstSheet='0')
    workbook.insert(0,views)
    sheets = workbook.find(f'{{{S}}}sheets')
    for i,name in enumerate(['AS 보고서','월별 입출고','업체별 현황','입고 상세'],3):
        ET.SubElement(sheets,f'{{{S}}}sheet',{'name':name,'sheetId':str(i),f'{{{R}}}id':f'rId{i}'})
    names = workbook.find(f'{{{S}}}definedNames')
    ET.SubElement(names,f'{{{S}}}definedName',{'name':'_xlnm.Print_Area','localSheetId':'2'}).text = "'AS 보고서'!$A$1:$N$42"
    parts['xl/workbook.xml'] = ET.tostring(workbook,encoding='utf-8')
    rels = ET.fromstring(parts['xl/_rels/workbook.xml.rels'])
    for i in range(3,7): ET.SubElement(rels,f'{{{P}}}Relationship',Id=f'rId{i}',Type=R+'/worksheet',Target=f'worksheets/sheet{i}.xml')
    ET.SubElement(rels,f'{{{P}}}Relationship',Id='rId7',Type=R+'/styles',Target='styles.xml')
    parts['xl/_rels/workbook.xml.rels'] = ET.tostring(rels,encoding='utf-8')
    parts['xl/styles.xml'] = STYLES
    for i,xml in tables.items(): parts[f'xl/worksheets/sheet{i}.xml'] = xml
    parts['xl/worksheets/_rels/sheet3.xml.rels'] = f'<Relationships xmlns="{P}"><Relationship Id="rId1" Type="{R}/drawing" Target="../drawings/drawing1.xml"/></Relationships>'
    parts['xl/drawings/drawing1.xml'] = drawing()
    parts['xl/drawings/_rels/drawing1.xml.rels'] = f'<Relationships xmlns="{P}">'+''.join(f'<Relationship Id="rId{i}" Type="{R}/chart" Target="../charts/chart{i}.xml"/>' for i in range(1,5))+'</Relationships>'
    for i,xml in enumerate(charts,1): parts[f'xl/charts/chart{i}.xml'] = xml
    types = ET.fromstring(parts['[Content_Types].xml'])
    additions = [('xl/styles.xml','application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml'),('xl/drawings/drawing1.xml','application/vnd.openxmlformats-officedocument.drawing+xml')]
    additions += [(f'xl/worksheets/sheet{i}.xml','application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml') for i in range(3,7)]
    additions += [(f'xl/charts/chart{i}.xml','application/vnd.openxmlformats-officedocument.drawingml.chart+xml') for i in range(1,5)]
    for part,content_type in additions: ET.SubElement(types,f'{{{CT}}}Override',PartName='/'+part,ContentType=content_type)
    parts['[Content_Types].xml'] = ET.tostring(types,encoding='utf-8')
    output = io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as target:
        for name,data in parts.items(): target.writestr(name,data)
    return output.getvalue()


def re_column(reference):
    return ''.join(ch for ch in reference if ch.isalpha())
