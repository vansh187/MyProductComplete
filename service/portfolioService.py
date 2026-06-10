from database.portfolioPersistence import portfolioPersistence
class portfolioService:

   
    def process_buyer(userId,symbol,quantity,price,cursor):
        portfolioPersistence.process_buyer(userId,symbol,quantity,price,cursor)


   
    def process_seller(userId,symbol,quantity,price,cursor):
        portfolioPersistence.process_seller(userId,symbol,quantity,price,cursor)


    def createUserHolding(order,userId):
        portfolioPersistence.createUserHolding(order,userId)

    def updateUserHoldings(order,userId,new_qty,round,new_avg):
        portfolioPersistence.updateUserHoldings(order,userId,new_qty,round,new_avg)

    def getPortfolioServiceforLoggedInUser(userId):
        return portfolioPersistence.getPortfolioServiceforLoggedInUser(userId)

