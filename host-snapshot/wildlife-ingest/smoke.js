const D=require("better-sqlite3"); const d=new D(":memory:"); console.log("sqlite OK", d.prepare("select 1 x").get());
