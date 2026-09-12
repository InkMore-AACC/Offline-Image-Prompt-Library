const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(require('path').join(__dirname,'../web/index.html'),'utf8');
const source=html.split('<script>')[1].split('</script>')[0];new vm.Script(source);
function context(){const nodes={scroll:{scrollTop:50},detail:{open:false},info:{replaceChildren(...v){this.value=v}},modalInfo:{replaceChildren(...v){this.value=v}},detailName:{},detailsEmpty:{}};const c={items:Array(180),total:300,failed:false,loading:false,editing:null,serial:0,focused:null,refreshBusy:false,knownRevision:0,knownLibrary:'x',document:{hidden:false},$:id=>nodes[id],msg:()=>{},api:async()=>({revision:1,libraryId:'x'}),foldersStatus:async()=>{},readReferences:async()=>{},infoContent:i=>i,load:async(reset=true)=>{c.items=Array(reset?60:120)},nodes};vm.createContext(c);vm.runInContext(source.slice(source.indexOf('async function refreshFocused'),source.indexOf('(async()=>{try{const s=await foldersStatus')),c);return c}
(async()=>{
 let c=context(),calls=0;c.load=async(reset=true)=>{if(++calls>4)throw Error('unbounded load loop');c.items=Array(reset?60:120);if(!reset)c.editing={}};await c.watchLibrary();assert.equal(calls,2);
 c=context();calls=0;c.load=async(reset=true)=>{calls++;if(reset)c.items=Array(60)};await c.watchLibrary();assert.equal(calls,2);
 c=context();c.focused={id:'a',annotation:'old'};c.api=async()=>({id:'a',annotation:'new'});await c.refreshFocused();assert.equal(c.nodes.info.value[0].annotation,'new');c.api=async()=>{throw Error('deleted')};await c.refreshFocused();assert.equal(c.focused,null);assert.equal(c.nodes.info.value.length,0);
 console.log('PASS: editing interrupts refresh; no-progress exits; detail data refreshes and deleted detail clears');
})().catch(e=>{console.error(e);process.exit(1)});
