/*!
 * ecg-generator.js — 心电图波形生成器
 * 用高斯脉冲叠加合成经典 ECG(P 波 / QRS 复合波 / T 波,可选 U 波),
 * 输出 SVG path 或完整动画 SVG 文件。
 *
 * 浏览器:<script src="./ecg-generator.js"></script> → window.ecg
 * Node CLI:
 *   node ecg-generator.js [--bpm 75] [--beats 1] [--amplitude 1] [--noise 0]
 *                         [--seed 1] [--include-u] [--out ekg-generated.svg]
 */
(function (global) {
  'use strict';

  // —— 波形参数:一个心动周期(75bpm,0.8s)内各波的高斯脉冲 ——
  // t: 波峰时间(周期内秒)  a: 幅度(以 R 波=1.0 为基准)  s: 上升支 σ  s2: 下降支 σ(不对称)
  // 间期核算(75bpm):PR 间期≈0.17s、QRS 时限≈0.09s、ST 段≈0.09s、
  //                 QT 间期≈0.37s、TP 段≈0.17s(含 U 波时 U 紧跟 T 后)
  var WAVES = [
    { name: 'P', t: 0.150, a:  0.12, s: 0.025 },  // P 波:小、圆钝,宽≈0.10s
    { name: 'Q', t: 0.275, a: -0.14, s: 0.006 },  // Q 波:宽<40ms、深<25% R
    { name: 'R', t: 0.300, a:  1.00, s: 0.006 },  // R 波:陡直主尖峰,峰时间≈37ms
    { name: 'S', t: 0.335, a: -0.22, s: 0.007 },  // S 波:窄谷
    { name: 'T', t: 0.540, a:  0.30, s: 0.050, s2: 0.035 }  // T 波:不对称(上升缓/下降陡≈1.5:1)
  ];
  var U_WAVE = { name: 'U', t: 0.700, a: 0.07, s: 0.030 };

  /**
   * 采样生成心电图点序列
   * @param {object} o
   * @param {number} [o.bpm=75]        心率(次/分),决定一个周期的时长
   * @param {number} [o.beats=1]       心搏个数
   * @param {number} [o.amplitude=1]   整体幅度缩放
   * @param {number} [o.baseline=0]     基线偏移(相对幅度单位)
   * @param {number} [o.drift=0.02]      基线漂移幅度(低频摆动,0 关闭)
   * @param {number} [o.noise=0]        每点随机抖动幅度(0~0.2 合适)
   * @param {number} [o.seed=1]         抖动随机种子(确定性,便于复现)
   * @param {boolean} [o.includeU=false] 是否加 U 波
   * @param {number} [o.sampleRate=300] 每秒采样数
   * @returns {{t:number,v:number}[]}
   */
  function ecgPoints(o) {
    o = o || {};
    var bpm = o.bpm || 75;
    var beats = o.beats || 1;
    var amplitude = o.amplitude == null ? 1 : o.amplitude;
    var baseline = o.baseline || 0;
    var drift = o.drift == null ? 0.02 : o.drift;
    var noise = o.noise || 0;
    var seed = o.seed || 1;
    var sampleRate = o.sampleRate || 300;
    var includeU = !!o.includeU;

    var period = 60 / bpm;                 // 每搏时长(秒)
    var waves = WAVES.concat(includeU ? [U_WAVE] : []);
    var n = Math.max(4, Math.round(period * sampleRate));

    // 确定性伪随机(LGC,与 Math.random 无关,可复现)
    var s = seed >>> 0 || 1;
    var rnd = function () {
      s = (s * 16807) % 2147483647;
      return s / 2147483647;
    };

    var pts = [];
    for (var b = 0; b < beats; b++) {
      var jitter = noise > 0 ? (rnd() - 0.5) * 2 * noise : 0;
      for (var i = 0; i < n; i++) {
        var t = i / sampleRate;
        var v = baseline;
        for (var w = 0; w < waves.length; w++) {
          var dt = t - waves[w].t;
          // 不对称高斯:上升支用 s,下降支用 s2(若有)
          var sg = dt < 0 ? waves[w].s : (waves[w].s2 || waves[w].s);
          v += waves[w].a * amplitude * Math.exp(-(dt * dt) / (2 * sg * sg));
        }
        v += drift * Math.sin(2 * Math.PI * (b + t / period) * 0.5);  // 低频基线漂移
        v += jitter;
        pts.push({ t: b * period + t, v: v });
      }
    }
    return pts;
  }

  /**
   * 生成 SVG path 数据(折线)
   * @param {object} o 同 ecgPoints,另有:
   * @param {number} [o.width=431.771]   viewBox 宽
   * @param {number} [o.height=431.771]  viewBox 高
   * @param {number} [o.pad=0.06]        留边比例(0~0.5)
   * @returns {{d:string, pts:object[], width:number, height:number, beats:number, bpm:number}}
   */
  function ecgPath(o) {
    o = o || {};
    var width = o.width || 431.771;
    var height = o.height || 431.771;
    var pad = o.pad == null ? 0.06 : o.pad;

    var pts = ecgPoints(o);
    var t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    var vMin = Infinity, vMax = -Infinity;
    for (var i = 0; i < pts.length; i++) {
      if (pts[i].v < vMin) vMin = pts[i].v;
      if (pts[i].v > vMax) vMax = pts[i].v;
    }
    var span = vMax - vMin || 1;
    var x0 = width * pad, x1 = width * (1 - pad);
    var yTop = height * pad, yBot = height * (1 - pad);

    var d = '';
    for (var j = 0; j < pts.length; j++) {
      var x = x0 + ((pts[j].t - t0) / (t1 - t0)) * (x1 - x0);
      var y = yBot - ((pts[j].v - vMin) / span) * (yBot - yTop);
      d += (j === 0 ? 'M' : 'L') + x.toFixed(2) + ',' + y.toFixed(2) + ' ';
    }
    return { d: d.trim(), pts: pts, width: width, height: height, beats: o.beats || 1, bpm: o.bpm || 75 };
  }

  /**
   * 生成完整动画 SVG(描边扫描 + 光点跟随,currentColor)
   * @param {object} o 同 ecgPath,另有:
   * @param {number} [o.svgWidth=22]   输出 SVG 宽度(px)
   * @param {number} [o.svgHeight=22]  输出 SVG 高度(px)
   * @param {number} [o.strokeWidth]   线宽(默认按 viewBox 431 基准 34 等比缩放)
   * @param {number} [o.dur]           单次扫描时长(秒,默认 2.6×beats)
   * @returns {string}
   */
  function buildSVG(o) {
    o = o || {};
    var r = ecgPath(o);
    var base = 431.771;
    var sw = o.strokeWidth != null ? o.strokeWidth : Math.round(34 * (r.width / base));
    var dur = o.dur != null ? o.dur : 2.6 * (o.beats || 1);
    var dotR = Math.max(6, Math.round(sw * 0.53));
    return '<?xml version="1.0" encoding="UTF-8"?>\n' +
      '<!-- 由 ecg-generator.js 生成 -->\n' +
      '<svg width="' + (o.svgWidth || 22) + 'px" height="' + (o.svgHeight || 22) + 'px" viewBox="0 0 ' + r.width + ' ' + r.height +
      '" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">\n' +
      '  <defs>\n' +
      '    <path id="ekgPath" d="' + r.d + '"/>\n' +
      '  </defs>\n' +
      '  <use href="#ekgPath" fill="none" stroke="currentColor" stroke-width="' + sw + '" stroke-linejoin="round" stroke-linecap="round"' +
      ' pathLength="1" stroke-dasharray="1" stroke-dashoffset="1">\n' +
      '    <animate attributeName="stroke-dashoffset" from="1" to="0" dur="' + dur.toFixed(2) + 's" repeatCount="indefinite" calcMode="linear"/>\n' +
      '  </use>\n' +
      '  <circle r="' + dotR + '" fill="currentColor">\n' +
      '    <animateMotion dur="' + dur.toFixed(2) + 's" repeatCount="indefinite" calcMode="linear" rotate="auto">\n' +
      '      <mpath href="#ekgPath"/>\n' +
      '    </animateMotion>\n' +
      '  </circle>\n' +
      '</svg>\n';
  }

  var api = { ecgPoints: ecgPoints, ecgPath: ecgPath, buildSVG: buildSVG, WAVES: WAVES };

  // Node(CommonJS)导出
  if (typeof module !== 'undefined' && module.exports) { module.exports = api; }
  // 浏览器全局
  if (global) { global.ecg = api; }
})(typeof window !== 'undefined' ? window : globalThis);

// —— CLI ——
if (typeof require !== 'undefined' && require.main === module) {
  var fs = require('fs');

  function parseArgs(argv) {
    var out = { bpm: 75, beats: 1, amplitude: 1, noise: 0, seed: 1, includeU: false, out: null, help: false };
    for (var i = 0; i < argv.length; i++) {
      var a = argv[i], next = function () { return argv[++i]; };
      switch (a) {
        case '--bpm':        out.bpm = parseFloat(next()); break;
        case '--beats':      out.beats = parseInt(next(), 10); break;
        case '--amplitude':  out.amplitude = parseFloat(next()); break;
        case '--noise':      out.noise = parseFloat(next()); break;
        case '--drift':      out.drift = parseFloat(next()); break;
        case '--seed':       out.seed = parseInt(next(), 10); break;
        case '--include-u':  out.includeU = true; break;
        case '--out':        out.out = next(); break;
        case '--width':      out.width = parseFloat(next()); break;
        case '--height':     out.height = parseFloat(next()); break;
        case '--svg-width':  out.svgWidth = parseFloat(next()); break;
        case '--svg-height': out.svgHeight = parseFloat(next()); break;
        case '--stroke':     out.strokeWidth = parseFloat(next()); break;
        case '--dur':        out.dur = parseFloat(next()); break;
        case '--help': case '-h':
          out.help = true; break;
        default:
          console.error('未知参数: ' + a); out.help = true;
      }
    }
    return out;
  }

  function printHelp() {
    console.log([
      'ecg-generator.js — 心电图波形生成器',
      '',
      '用法: node ecg-generator.js [选项]',
      '',
      '选项:',
      '  --bpm <num>        心率,默认 75',
      '  --beats <num>      心搏个数,默认 1',
      '  --amplitude <num>  幅度缩放,默认 1',
      '  --noise <num>      抖动幅度,默认 0',
      '  --drift <num>      基线漂移幅度,默认 0.02(0 关闭)',
      '  --seed <num>       随机种子,默认 1',
      '  --include-u        包含 U 波',
      '  --out <file>       输出 SVG 文件,默认 ekg-generated.svg',
      '  --width/--height   生成 viewBox 尺寸,默认 431.771',
      '  --svg-width/--svg-height  输出文件像素尺寸,默认 22',
      '  --stroke <num>     线宽(单位),默认按 431 基准 34 等比',
      '  --dur <num>        单次扫描秒数,默认 2.6×beats',
      '  --help             显示帮助',
      '',
      '示例: node ecg-generator.js --bpm 120 --beats 2 --noise 0.05 --out ekg-fast.svg'
    ].join('\n'));
  }

  var args = parseArgs(process.argv.slice(2));
  if (args.help) { printHelp(); process.exit(0); }

  var svg = globalThis.ecg.buildSVG(args);
  var out = args.out || 'ekg-generated.svg';
  fs.writeFileSync(out, svg);
  console.log('✔ 已生成 ' + out +
    ' (bpm=' + args.bpm + ', beats=' + args.beats +
    ', amplitude=' + args.amplitude + ', noise=' + args.noise +
    (args.includeU ? ', U 波' : '') + ')');
}
