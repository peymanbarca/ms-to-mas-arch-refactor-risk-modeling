// weights
function getWeights(){

    return {

        fanout:
        parseFloat(document.getElementById("wFanout").value),

        bc:
        parseFloat(document.getElementById("wBC").value),

        ccyl:
        parseFloat(document.getElementById("wCCyl").value),

        ccog:
        parseFloat(document.getElementById("wCCog").value),

        tprop:
        parseFloat(document.getElementById("wTProp").value)

    };

}

function validateWeights(){

    const W=getWeights();


    const sum =

    W.fanout+
    W.bc+
    W.ccyl+
    W.ccog+
    W.tprop;


    document.getElementById("weightSum")
    .innerHTML=sum.toFixed(2);



    if(Math.abs(sum-1)>0.001){

    alert(
    "Weight sum must equal 1.0"
    );

    return false;

    }


    alert(
    "Weights validated successfully"
    );


    computeScores();

    return true;

}

function normalizeColumn(values){

    let min = Math.min(...values);
    let max = Math.max(...values);

    // avoid division by zero
    if(max === min){
        return values.map(()=>0);
    }

    return values.map(v =>
        (v-min)/(max-min)
    );

}



function computeScores(){

    const W=getWeights();

    const tbody=document.querySelector("#rankingTable tbody");

    const rows=[...tbody.rows];


    /*
       Extract raw values
    */

    let fanouts=[];
    let bcs=[];
    let ccyls=[];
    let ccogs=[];
    let tprops=[];


    rows.forEach(row=>{

        fanouts.push(
            parseFloat(
                row.querySelector(".fanout").value
            )
        );

        bcs.push(
            parseFloat(
                row.querySelector(".bc").value
            )
        );

        ccyls.push(
            parseFloat(
                row.querySelector(".ccyl").value
            )
        );

        ccogs.push(
            parseFloat(
                row.querySelector(".ccog").value
            )
        );

        tprops.push(
            parseFloat(
                row.querySelector(".tprop").value
            )
        );

    });



    /*
       Normalize each metric column
    */

    let nFanout=normalizeColumn(fanouts);

    let nBC=normalizeColumn(bcs);

    let nCCyl=normalizeColumn(ccyls);

    let nCCog=normalizeColumn(ccogs);

    let nTProp=normalizeColumn(tprops);



    /*
       Calculate weighted risk score
    */

    rows.forEach((row,index)=>{


        let score =

        W.fanout*nFanout[index]+

        W.bc*nBC[index]+

        W.ccyl*nCCyl[index]+

        W.ccog*nCCog[index]+

        W.tprop*nTProp[index];


        row.dataset.score=score;


        row.querySelector(".score")
        .innerHTML=score.toFixed(3);



    });



    /*
       Sort ascending:
       lowest risk first
    */

    rows.sort(

        (a,b)=>

        parseFloat(a.dataset.score)
        -
        parseFloat(b.dataset.score)

    );



    /*
       Update table
    */

    tbody.innerHTML="";


    rows.forEach((row,index)=>{


        row.querySelector(".rank")
        .innerHTML=index+1;


        tbody.appendChild(row);


    });


}

computeScores();


function validatePredicates(){

    let checked=0;

    document.querySelectorAll("#step1 input[type=checkbox]").forEach(c=>{

    if(c.checked) checked++;

    });

    if(checked===0){

    alert("Select at least one predicate.");

    return;

}

document.getElementById("step2").classList.remove("disabled");

}

function validateGovernance(){

    document.getElementById("step3").classList.remove("disabled");

}


document.getElementById("start").onclick=function(){


    let payload={


    predicates:{


    qa:
    document.getElementById("qa").checked,


    qa_threshold:
    document.getElementById("qaThreshold").value,


    latency:
    document.getElementById("latency").checked,

    latency_threshold:
    document.getElementById("latencyThreshold").value,

    failure:
    document.getElementById("failure").checked,

    failure_threshold:
    document.getElementById("failureThreshold").value

    },



    governance_mode:

    document.querySelector(
    "input[name=gov]:checked"
    ).value,



    governance_thresholds:{


    beta:
    document.getElementById("beta").value,


    gmid:
    document.getElementById("gmid").value,


    deltaL:
    document.getElementById("deltaL").value,


    deltaSLO:
    document.getElementById("deltaSLO").value,


    deltaTProp:
    document.getElementById("deltaTP").value,


    gpost:
    document.getElementById("gpost").value


    },



    runtime:{


    model:"llama3.2:3b",

    temperature:0.2,

    R:5000,

    concurrency:10


    },



    ranking_weights:getWeights(),



    ranked_services:

    getCurrentRanking()


    };



    fetch(
    "/run-experiment",
    {

    method:"POST",

    headers:{
    "Content-Type":"application/json"
    },

    body:
    JSON.stringify(payload)

    }

    );



    startLogRefresh();


};

function getCurrentRanking(){


    let rows=[
    ...document.querySelectorAll(
    "#rankingTable tbody tr"
    )
    ];


    return rows.map(row=>{


    return {

    rank:
    row.querySelector(".rank").innerText,


    service:
    row.children[1].innerText,


    score:
    row.querySelector(".score").innerText


    };


    });


}

let logTimer;


function startLogRefresh(){


    logTimer=setInterval(()=>{


    fetch("/logs")

    .then(
    r=>r.json()
    )

    .then(
    data=>{


    let box=document.getElementById(
    "logs"
    );


    box.textContent=data.logs;


    box.scrollTop=
    box.scrollHeight;


    }

    );


    },1000);


}