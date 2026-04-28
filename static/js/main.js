

console.log("MatrixMatch frontend loaded.");

// CTA on landing page -> go to login
document.querySelector(".cta-btn")?.addEventListener("click", (e) => {
    e.preventDefault();
    window.location.href = "/login";
});

// "Sign in" link on Register page -> go to login
document.querySelector(".js-go-login")?.addEventListener("click", (e) => {
    e.preventDefault();
    window.location.href = "/login";
});