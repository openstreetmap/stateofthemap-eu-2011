function blinzel(x) {
if (document.getElementById('head_'+x).className == 'head_open') {
document.getElementById('head_'+x).className = 'head_closed';
document.getElementById('box_'+x).className = 'box_closed';
}
else {
document.getElementById('head_'+x).className = 'head_open';
document.getElementById('box_'+x).className = 'box_open';
}
} 